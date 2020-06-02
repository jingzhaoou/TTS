import argparse
import glob
import os
import sys
import time
import traceback

import torch
from torch.utils.data import DataLoader

from inspect import signature

from TTS.utils.audio import AudioProcessor
from TTS.utils.generic_utils import (KeepAverage, count_parameters,
                                     create_experiment_folder, get_git_branch,
                                     remove_experiment_folder, set_init_dict)
from TTS.utils.io import copy_config_file, load_config
from TTS.utils.radam import RAdam
from TTS.utils.tensorboard_logger import TensorboardLogger
from TTS.utils.training import NoamLR
from TTS.vocoder.datasets.gan_dataset import GANDataset
from TTS.vocoder.datasets.preprocess import load_wav_data
# from distribute import (DistributedSampler, apply_gradient_allreduce,
#                         init_distributed, reduce_tensor)
from TTS.vocoder.layers.losses import DiscriminatorLoss, GeneratorLoss
from TTS.vocoder.utils.io import save_checkpoint, save_best_model
from TTS.vocoder.utils.console_logger import ConsoleLogger
from TTS.vocoder.utils.generic_utils import (check_config, plot_results,
                                             setup_discriminator,
                                             setup_generator)

torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True
torch.manual_seed(54321)
use_cuda = torch.cuda.is_available()
num_gpus = torch.cuda.device_count()
print(" > Using CUDA: ", use_cuda)
print(" > Number of GPUs: ", num_gpus)


def setup_loader(ap, is_val=False, verbose=False):
    if is_val and not c.run_eval:
        loader = None
    else:
        dataset = GANDataset(ap=ap,
                             items=eval_data if is_val else train_data,
                             seq_len=c.seq_len,
                             hop_len=ap.hop_length,
                             pad_short=c.pad_short,
                             conv_pad=c.conv_pad,
                             is_training=not is_val,
                             return_segments=False if is_val else True,
                             use_noise_augment=c.use_noise_augment,
                             use_cache=c.use_cache,
                             verbose=verbose)
        # sampler = DistributedSampler(dataset) if num_gpus > 1 else None
        loader = DataLoader(dataset,
                            batch_size=1 if is_val else c.batch_size,
                            shuffle=False,
                            drop_last=False,
                            sampler=None,
                            num_workers=c.num_val_loader_workers
                            if is_val else c.num_loader_workers,
                            pin_memory=False)
    return loader


def format_data(data):
    if isinstance(data[0], list):
        # setup input data
        c_G, x_G = data[0]
        c_D, x_D = data[1]

        # dispatch data to GPU
        if use_cuda:
            c_G = c_G.cuda(non_blocking=True)
            x_G = x_G.cuda(non_blocking=True)
            c_D = c_D.cuda(non_blocking=True)
            x_D = x_D.cuda(non_blocking=True)

        return c_G, x_G, c_D, x_D

    # return a whole audio segment
    c, x = data
    if use_cuda:
        c = c.cuda(non_blocking=True)
        x = x.cuda(non_blocking=True)
    return c, x


def train(model_G, criterion_G, optimizer_G, model_D, criterion_D, optimizer_D,
          scheduler_G, scheduler_D, ap, global_step, epoch):
    data_loader = setup_loader(ap, is_val=False, verbose=(epoch == 0))
    model_G.train()
    model_D.train()
    epoch_time = 0
    keep_avg = KeepAverage()
    if use_cuda:
        batch_n_iter = int(
            len(data_loader.dataset) / (c.batch_size * num_gpus))
    else:
        batch_n_iter = int(len(data_loader.dataset) / c.batch_size)
    end_time = time.time()
    c_logger.print_train_start()
    for num_iter, data in enumerate(data_loader):
        start_time = time.time()

        # format data
        c_G, y_G, c_D, y_D = format_data(data)
        loader_time = time.time() - end_time

        global_step += 1

        # get current learning rates
        current_lr_G = list(optimizer_G.param_groups)[0]['lr']
        current_lr_D = list(optimizer_D.param_groups)[0]['lr']

        ##############################
        # GENERATOR
        ##############################

        # generator pass
        optimizer_G.zero_grad()
        y_hat = model_G(c_G)
        y_hat_sub = None
        y_G_sub = None

        # PQMF formatting
        if y_hat.shape[1] > 1:
            y_hat_sub = y_hat
            y_hat = model_G.pqmf_synthesis(y_hat)
            y_G_sub = model_G.pqmf_analysis(y_G)

        if global_step > c.steps_to_start_discriminator:

            # run D with or without cond. features
            if len(signature(model_D.forward).parameters) == 2:
                D_out_fake = model_D(y_hat, c_G)
            else:
                D_out_fake = model_D(y_hat)
            D_out_real = None

            if c.use_feat_match_loss:
                with torch.no_grad():
                    D_out_real = model_D(y_G)

            # format D outputs
            if isinstance(D_out_fake, tuple):
                scores_fake, feats_fake = D_out_fake
                if D_out_real is None:
                    scores_real, feats_real = None, None
                else:
                    scores_real, feats_real = D_out_real
            else:
                scores_fake = D_out_fake
                scores_real = D_out_real
        else:
            scores_fake, feats_fake, feats_real = None, None, None

        # compute losses
        loss_G_dict = criterion_G(y_hat, y_G, scores_fake, feats_fake,
                                  feats_real, y_hat_sub, y_G_sub)
        loss_G = loss_G_dict['G_loss']

        # optimizer generator
        loss_G.backward()
        if c.gen_clip_grad > 0:
            torch.nn.utils.clip_grad_norm_(model_G.parameters(),
                                           c.gen_clip_grad)
        optimizer_G.step()

        # setup lr
        if c.noam_schedule:
            scheduler_G.step()

        loss_dict = dict()
        for key, value in loss_G_dict.items():
            loss_dict[key] = value.item()

        ##############################
        # DISCRIMINATOR
        ##############################
        if global_step > c.steps_to_start_discriminator:
            # discriminator pass
            with torch.no_grad():
                y_hat = model_G(c_D)

            # PQMF formatting
            if y_hat.shape[1] > 1:
                y_hat = model_G.pqmf_synthesis(y_hat)

            optimizer_D.zero_grad()

            # run D with or without cond. features
            if len(signature(model_D.forward).parameters) == 2:
                D_out_fake = model_D(y_hat.detach(), c_D)
                D_out_real = model_D(y_D, c_D)
            else:
                D_out_fake = model_D(y_hat.detach())
                D_out_real = model_D(y_D)

            # format D outputs
            if isinstance(D_out_fake, tuple):
                scores_fake, feats_fake = D_out_fake
                if D_out_real is None:
                    scores_real, feats_real = None, None
                else:
                    scores_real, feats_real = D_out_real
            else:
                scores_fake = D_out_fake
                scores_real = D_out_real

            # compute losses
            loss_D_dict = criterion_D(scores_fake, scores_real)
            loss_D = loss_D_dict['D_loss']

            # optimizer discriminator
            loss_D.backward()
            if c.disc_clip_grad > 0:
                torch.nn.utils.clip_grad_norm_(model_D.parameters(),
                                               c.disc_clip_grad)
            optimizer_D.step()

            # setup lr
            if c.noam_schedule:
                scheduler_D.step()

            for key, value in loss_D_dict.items():
                loss_dict[key] = value.item()

        step_time = time.time() - start_time
        epoch_time += step_time

        # update avg stats
        update_train_values = dict()
        for key, value in loss_dict.items():
            update_train_values['avg_' + key] = value
        update_train_values['avg_loader_time'] = loader_time
        update_train_values['avg_step_time'] = step_time
        keep_avg.update_values(update_train_values)

        # print training stats
        if global_step % c.print_step == 0:
            c_logger.print_train_step(batch_n_iter, num_iter, global_step,
                                      step_time, loader_time, current_lr_G,
                                      loss_dict, keep_avg.avg_values)

        # plot step stats
        if global_step % 10 == 0:
            iter_stats = {
                "lr_G": current_lr_G,
                "lr_D": current_lr_D,
                "step_time": step_time
            }
            iter_stats.update(loss_dict)
            tb_logger.tb_train_iter_stats(global_step, iter_stats)

        # save checkpoint
        if global_step % c.save_step == 0:
            if c.checkpoint:
                # save model
                save_checkpoint(model_G,
                                optimizer_G,
                                model_D,
                                optimizer_D,
                                global_step,
                                epoch,
                                OUT_PATH,
                                model_losses=loss_dict)

            # compute spectrograms
            figures = plot_results(y_hat, y_G, ap, global_step,
                                   'train')
            tb_logger.tb_train_figures(global_step, figures)

            # Sample audio
            sample_voice = y_hat[0].squeeze(0).detach().cpu().numpy()
            tb_logger.tb_train_audios(global_step,
                                      {'train/audio': sample_voice},
                                      c.audio["sample_rate"])
        end_time = time.time()

    # print epoch stats
    c_logger.print_train_epoch_end(global_step, epoch, epoch_time, keep_avg)

    # Plot Training Epoch Stats
    epoch_stats = {"epoch_time": epoch_time}
    epoch_stats.update(keep_avg.avg_values)
    tb_logger.tb_train_epoch_stats(global_step, epoch_stats)
    # TODO: plot model stats
    # if c.tb_model_param_stats:
    # tb_logger.tb_model_weights(model, global_step)
    return keep_avg.avg_values, global_step


@torch.no_grad()
def evaluate(model_G, criterion_G, model_D, ap, global_step, epoch):
    data_loader = setup_loader(ap, is_val=True, verbose=(epoch == 0))
    model_G.eval()
    model_D.eval()
    epoch_time = 0
    keep_avg = KeepAverage()
    end_time = time.time()
    c_logger.print_eval_start()
    for num_iter, data in enumerate(data_loader):
        start_time = time.time()

        # format data
        c_G, y_G = format_data(data)
        loader_time = time.time() - end_time

        global_step += 1

        ##############################
        # GENERATOR
        ##############################

        # generator pass
        y_hat = model_G(c_G)
        y_hat_sub = None
        y_G_sub = None

        # PQMF formatting
        if y_hat.shape[1] > 1:
            y_hat_sub = y_hat
            y_hat = model_G.pqmf_synthesis(y_hat)
            y_G_sub = model_G.pqmf_analysis(y_G)

        D_out_fake = model_D(y_hat)
        D_out_real = None
        if c.use_feat_match_loss:
            with torch.no_grad():
                D_out_real = model_D(y_G)

        # format D outputs
        if isinstance(D_out_fake, tuple):
            scores_fake, feats_fake = D_out_fake
            if D_out_real is None:
                feats_real = None
            else:
                _, feats_real = D_out_real
        else:
            scores_fake = D_out_fake

        # compute losses
        loss_G_dict = criterion_G(y_hat, y_G, scores_fake, feats_fake,
                                  feats_real, y_hat_sub, y_G_sub)

        loss_dict = dict()
        for key, value in loss_G_dict.items():
            loss_dict[key] = value.item()

        step_time = time.time() - start_time
        epoch_time += step_time

        # update avg stats
        update_eval_values = dict()
        for key, value in loss_G_dict.items():
            update_eval_values['avg_' + key] = value.item()
        update_eval_values['avg_loader_time'] = loader_time
        update_eval_values['avgP_step_time'] = step_time
        keep_avg.update_values(update_eval_values)

        # print eval stats
        if c.print_eval:
            c_logger.print_eval_step(num_iter, loss_dict, keep_avg.avg_values)

    # compute spectrograms
    figures = plot_results(y_hat, y_G, ap, global_step, 'eval')
    tb_logger.tb_eval_figures(global_step, figures)

    # Sample audio
    sample_voice = y_hat[0].squeeze(0).detach().cpu().numpy()
    tb_logger.tb_eval_audios(global_step, {'eval/audio': sample_voice},
                             c.audio["sample_rate"])

    # synthesize a full voice
    data_loader.return_segments = False

    tb_logger.tb_eval_stats(global_step, keep_avg.avg_values)

    return keep_avg.avg_values


# FIXME: move args definition/parsing inside of main?
def main(args):  # pylint: disable=redefined-outer-name
    # pylint: disable=global-variable-undefined
    global train_data, eval_data
    eval_data, train_data = load_wav_data(c.data_path, c.eval_split_size)

    # setup audio processor
    ap = AudioProcessor(**c.audio)
    # DISTRUBUTED
    # if num_gpus > 1:
    # init_distributed(args.rank, num_gpus, args.group_id,
    #  c.distributed["backend"], c.distributed["url"])

    # setup models
    model_gen = setup_generator(c)
    model_disc = setup_discriminator(c)

    # setup optimizers
    optimizer_gen = RAdam(model_gen.parameters(), lr=c.lr_gen, weight_decay=0)
    optimizer_disc = RAdam(model_disc.parameters(),
                           lr=c.lr_disc,
                           weight_decay=0)

    # setup criterion
    criterion_gen = GeneratorLoss(c)
    criterion_disc = DiscriminatorLoss(c)

    if args.restore_path:
        checkpoint = torch.load(args.restore_path, map_location='cpu')
        try:
            model_gen.load_state_dict(checkpoint['model'])
            optimizer_gen.load_state_dict(checkpoint['optimizer'])
            model_disc.load_state_dict(checkpoint['model_disc'])
            optimizer_disc.load_state_dict(checkpoint['optimizer_disc'])
        except:
            print(" > Partial model initialization.")
            model_dict = model_gen.state_dict()
            model_dict = set_init_dict(model_dict, checkpoint['model'], c)
            model_gen.load_state_dict(model_dict)

            model_dict = model_disc.state_dict()
            model_dict = set_init_dict(model_dict, checkpoint['model_disc'], c)
            model_disc.load_state_dict(model_dict)
            del model_dict

        # reset lr if not countinuining training.
        for group in optimizer_gen.param_groups:
            group['lr'] = c.lr_gen

        for group in optimizer_disc.param_groups:
            group['lr'] = c.lr_disc

        print(" > Model restored from step %d" % checkpoint['step'],
              flush=True)
        args.restore_step = checkpoint['step']
    else:
        args.restore_step = 0

    if use_cuda:
        model_gen.cuda()
        criterion_gen.cuda()
        model_disc.cuda()
        criterion_disc.cuda()

    # DISTRUBUTED
    # if num_gpus > 1:
    #     model = apply_gradient_allreduce(model)

    if c.noam_schedule:
        scheduler_gen = NoamLR(optimizer_gen,
                               warmup_steps=c.warmup_steps_gen,
                               last_epoch=args.restore_step - 1)
        scheduler_disc = NoamLR(optimizer_disc,
                                warmup_steps=c.warmup_steps_gen,
                                last_epoch=args.restore_step - 1)
    else:
        scheduler_gen, scheduler_disc = None, None

    num_params = count_parameters(model_gen)
    print(" > Generator has {} parameters".format(num_params), flush=True)
    num_params = count_parameters(model_disc)
    print(" > Discriminator has {} parameters".format(num_params), flush=True)

    if 'best_loss' not in locals():
        best_loss = float('inf')

    global_step = args.restore_step
    for epoch in range(0, c.epochs):
        c_logger.print_epoch_start(epoch, c.epochs)
        _, global_step = train(model_gen, criterion_gen, optimizer_gen,
                               model_disc, criterion_disc, optimizer_disc,
                               scheduler_gen, scheduler_disc, ap, global_step,
                               epoch)
        eval_avg_loss_dict = evaluate(model_gen, criterion_gen, model_disc, ap,
                                      global_step, epoch)
        c_logger.print_epoch_end(epoch, eval_avg_loss_dict)
        target_loss = eval_avg_loss_dict[c.target_loss]
        best_loss = save_best_model(target_loss,
                                    best_loss,
                                    model_gen,
                                    optimizer_gen,
                                    model_disc,
                                    optimizer_disc,
                                    global_step,
                                    epoch,
                                    OUT_PATH,
                                    model_losses=eval_avg_loss_dict)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--continue_path',
        type=str,
        help=
        'Training output folder to continue training. Use to continue a training. If it is used, "config_path" is ignored.',
        default='',
        required='--config_path' not in sys.argv)
    parser.add_argument(
        '--restore_path',
        type=str,
        help='Model file to be restored. Use to finetune a model.',
        default='')
    parser.add_argument('--config_path',
                        type=str,
                        help='Path to config file for training.',
                        required='--continue_path' not in sys.argv)
    parser.add_argument('--debug',
                        type=bool,
                        default=False,
                        help='Do not verify commit integrity to run training.')

    # DISTRUBUTED
    parser.add_argument(
        '--rank',
        type=int,
        default=0,
        help='DISTRIBUTED: process rank for distributed training.')
    parser.add_argument('--group_id',
                        type=str,
                        default="",
                        help='DISTRIBUTED: process group id.')
    args = parser.parse_args()

    if args.continue_path != '':
        args.output_path = args.continue_path
        args.config_path = os.path.join(args.continue_path, 'config.json')
        list_of_files = glob.glob(
            args.continue_path +
            "/*.pth.tar")  # * means all if need specific format then *.csv
        latest_model_file = max(list_of_files, key=os.path.getctime)
        args.restore_path = latest_model_file
        print(f" > Training continues for {args.restore_path}")

    # setup output paths and read configs
    c = load_config(args.config_path)
    check_config(c)
    _ = os.path.dirname(os.path.realpath(__file__))

    OUT_PATH = args.continue_path
    if args.continue_path == '':
        OUT_PATH = create_experiment_folder(c.output_path, c.run_name,
                                            args.debug)

    AUDIO_PATH = os.path.join(OUT_PATH, 'test_audios')

    c_logger = ConsoleLogger()

    if args.rank == 0:
        os.makedirs(AUDIO_PATH, exist_ok=True)
        new_fields = {}
        if args.restore_path:
            new_fields["restore_path"] = args.restore_path
        new_fields["github_branch"] = get_git_branch()
        copy_config_file(args.config_path,
                         os.path.join(OUT_PATH, 'config.json'), new_fields)
        os.chmod(AUDIO_PATH, 0o775)
        os.chmod(OUT_PATH, 0o775)

        LOG_DIR = OUT_PATH
        tb_logger = TensorboardLogger(LOG_DIR, model_name='VOCODER')

        # write model desc to tensorboard
        tb_logger.tb_add_text('model-description', c['run_description'], 0)

    try:
        main(args)
    except KeyboardInterrupt:
        remove_experiment_folder(OUT_PATH)
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-except
        remove_experiment_folder(OUT_PATH)
        traceback.print_exc()
        sys.exit(1)
