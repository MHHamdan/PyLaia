#!/usr/bin/env python

from __future__ import division

import os

import torch
from torch.nn.utils.rnn import PackedSequence, pack_padded_sequence

import laia.data
import laia.engine
import laia.logging
import laia.nn
import laia.utils
from dortmund_utils import build_conv_model, DortmundImageToTensor
from laia.engine.feeders import ImageFeeder, ItemFeeder
from laia.engine.triggers import (Any, NumEpochs,
                                  MeterStandardDeviation)
from laia.plugins.arguments import add_argument, add_defaults, args

logger = laia.logging.get_logger('laia.egs.washington.train_ctc')


class SaveModelCheckpointHook(object):
    def __init__(self, meter, filename):
        self._meter = meter
        self._lowest = float('inf')
        self._filename = os.path.normpath(filename)
        dirname = os.path.dirname(self._filename)
        if dirname and not os.path.exists(dirname):
            os.makedirs(dirname)

    def _save(self, model_state_dict, optimizer_state_dict):
        torch.save({
            'model': model_state_dict,
            'optimizer': optimizer_state_dict
        }, self._filename)

    def __call__(self, caller, **_):
        assert isinstance(caller, laia.engine.Trainer)
        if self._meter.value < self._lowest:
            self._lowest = self._meter.value
            logger.info('New lowest validation CER: {:5.1%}', self._lowest)
            self._save(caller.model.state_dict(),
                       caller.optimizer.state_dict())


def build_model(num_outputs,
                adaptive_pool_height=16,
                lstm_hidden_size=128,
                lstm_num_layers=1):
    class RNNWrapper(torch.nn.Module):
        def __init__(self, module):
            super(RNNWrapper, self).__init__()
            self._module = module

        def forward(self, input):
            if isinstance(input, tuple):
                input = input[0]
            return self._module(input)

        def __repr__(self):
            return repr(self._module)

    class PackedSequenceWrapper(torch.nn.Module):
        def __init__(self, module):
            super(PackedSequenceWrapper, self).__init__()
            self._module = module

        def forward(self, input):
            if isinstance(input, PackedSequence):
                x, xs = input.data, input.batch_sizes
            else:
                x, xs = input, None

            y = self._module(x)
            if xs is None:
                return pack_padded_sequence(input=y, lengths=[y.size(0)])
            else:
                return PackedSequence(data=y, batch_sizes=xs)

        def __repr__(self):
            return repr(self._module)

    m = build_conv_model()
    m.add_module('adap_pool', laia.nn.AdaptiveAvgPool2d(
        output_size=(adaptive_pool_height, None)))
    m.add_module('collapse', laia.nn.ImageToSequence(return_packed=True))
    # 512 = number of filters in the last layer of Dortmund's model
    m.add_module('blstm', torch.nn.LSTM(
        input_size=512 * adaptive_pool_height,
        hidden_size=lstm_hidden_size,
        num_layers=lstm_num_layers,
        dropout=0.5,
        bidirectional=True))
    m.add_module('linear',
                 RNNWrapper(PackedSequenceWrapper(
                     torch.nn.Linear(2 * lstm_hidden_size, num_outputs))))
    return m


if __name__ == '__main__':
    add_defaults('gpu', 'max_epochs', 'max_updates', 'num_samples_per_epoch',
                 'seed',
                 'train_loss_std_window_size', 'train_loss_std_threshold',
                 'valid_cer_std_window_size', 'valid_cer_std_threshold',
                 # Override default values for these arguments, but use the
                 # same help/checks:
                 batch_size=1,
                 learning_rate=0.015,
                 momentum=0.9,
                 num_iterations_per_update=10,
                 show_progress_bar=True,
                 use_distortions=True,
                 weight_l2_penalty=0.00005)
    add_argument('--save_checkpoint', type=str, default='model.ckpt',
                 help='Filename of the output model checkpoint')
    add_argument('--load_checkpoint', type=str, default=None,
                 help='Load model parameters from this checkpoint')
    add_argument('syms', help='Symbols table mapping from strings to integers')
    add_argument('tr_img_dir', help='Directory containing word images')
    add_argument('tr_txt_table',
                 help='Character transcriptions of each training image')
    add_argument('va_txt_table',
                 help='Character transcriptions of each validation image')
    args = args()
    laia.random.manual_seed(args.seed)

    syms = laia.utils.SymbolsTable(args.syms)
    model = build_model(num_outputs=len(syms))
    if args.gpu > 0:
        model = model.cuda(args.gpu - 1)
    else:
        model = model.cpu()

    optimizer = torch.optim.SGD(params=model.parameters(),
                                lr=args.learning_rate,
                                momentum=args.momentum,
                                weight_decay=args.weight_l2_penalty)

    if args.load_checkpoint:
        logger.info('Loading parameters from {!r}', args.load_checkpoint)
        saved_dict = torch.load(args.load_checkpoint)
        if 'model' in saved_dict and 'optimizer' in saved_dict:
            model.load_state_dict(saved_dict['model'])
            optimizer.load_state_dict(saved_dict['optimizer'])
        else:
            model.load_state_dict(saved_dict)

    # If --use_distortions is given, apply the same affine distortions used by
    # Dortmund University.
    if args.use_distortions:
        tr_img_transform = DortmundImageToTensor()
    else:
        tr_img_transform = laia.utils.ImageToTensor()

    # Training data
    tr_ds = laia.data.TextImageFromTextTableDataset(
        args.tr_txt_table, args.tr_img_dir,
        img_transform=tr_img_transform,
        txt_transform=laia.utils.TextToTensor(syms))
    if args.num_samples_per_epoch is None:
        tr_ds_loader = laia.data.ImageDataLoader(
            tr_ds, image_channels=1, batch_size=1, num_workers=8, shuffle=True)
    else:
        tr_ds_loader = laia.data.ImageDataLoader(
            tr_ds, image_channels=1, batch_size=1, num_workers=8,
            sampler=laia.data.FixedSizeSampler(tr_ds,
                                               args.num_samples_per_epoch))

    # Validation data
    va_ds = laia.data.TextImageFromTextTableDataset(
        args.va_txt_table, args.tr_img_dir,
        img_transform=laia.utils.ImageToTensor(),
        txt_transform=laia.utils.TextToTensor(syms))
    va_ds_loader = laia.data.ImageDataLoader(dataset=va_ds,
                                             image_channels=1,
                                             batch_size=args.batch_size,
                                             num_workers=8)

    trainer = laia.engine.Trainer(
        model=model,
        criterion=None,
        optimizer=optimizer,
        data_loader=tr_ds_loader,
        batch_input_fn=ImageFeeder(device=args.gpu,
                                   keep_padded_tensors=False,
                                   parent_feeder=ItemFeeder('img')),
        batch_target_fn=ItemFeeder('txt'),
        progress_bar='Train' if args.show_progress_bar else False)

    evaluator = laia.engine.Evaluator(
        model=model,
        data_loader=va_ds_loader,
        batch_input_fn=ImageFeeder(device=args.gpu,
                                   keep_padded_tensors=False,
                                   parent_feeder=ItemFeeder('img')),
        batch_target_fn=ItemFeeder('txt'),
        progress_bar='Valid' if args.show_progress_bar else False)

    engine_wrapper = laia.engine.HtrEngineWrapper(trainer, evaluator)

    # List of early stop triggers.
    # If any of these returns True, training will stop.
    early_stop_triggers = []

    # Configure NumEpochs trigger
    if args.max_epochs and args.max_epochs > 0:
        early_stop_triggers.append(
            NumEpochs(trainer=trainer, num_epochs=args.max_epochs))

    # Configure MeterStandardDeviation trigger to monitor validation CER
    if args.valid_cer_std_window_size and args.valid_cer_std_threshold:
        early_stop_triggers.append(
            MeterStandardDeviation(
                meter=engine_wrapper.valid_cer,
                threshold=args.valid_cer_std_threshold,
                num_values_to_keep=args.valid_cer_std_window_size))

    trainer.set_early_stop_trigger(Any(*early_stop_triggers))
    trainer.set_num_iterations_per_update(args.num_iterations_per_update)

    if args.save_checkpoint:
        filename_va = args.save_checkpoint + '-valid-lowest-cer'
        trainer.add_hook(
            trainer.ON_EPOCH_END,
            SaveModelCheckpointHook(engine_wrapper.valid_cer, filename_va))

    # Start training
    with torch.cuda.device(args.gpu - 1):
        engine_wrapper.run()

    # Save model parameters after training
    if args.save_checkpoint:
        torch.save({
            'model': model.state_dict(),
            'optimizer': optimizer.state_dict()
        }, args.save_checkpoint)