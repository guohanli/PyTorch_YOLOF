from __future__ import division

import os
import argparse
import time
from copy import deepcopy

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

from utils import distributed_utils
from utils.com_flops_params import FLOPs_and_Params
from utils.misc import CollateFunc, get_total_grad_norm, vis_data
from utils.misc import build_dataset, build_dataloader
from utils.solver.optimizer import build_optimizer
from utils.solver.warmup_schedule import build_warmup

from models.yolof import build_model
from models.yolof.criterion import build_criterion

from config.yolof_config import yolof_config


def parse_args():
    parser = argparse.ArgumentParser(description='YOLOF Detection')
    # basic
    parser.add_argument('--cuda', action='store_true', default=False,
                        help='use cuda.')
    parser.add_argument('-bs', '--batch_size', default=16, type=int, 
                        help='Batch size for training')
    parser.add_argument('--schedule', type=str, default='1x', choices=['1x', '2x', '3x', '9x'],
                        help='training schedule. Attention, 9x is designed for YOLOF53-DC5.')
    parser.add_argument('--num_workers', default=4, type=int, 
                        help='Number of workers used in dataloading')
    parser.add_argument('--num_gpu', default=1, type=int, 
                        help='Number of GPUs to train')
    parser.add_argument('--eval_epoch', type=int,
                            default=2, help='interval between evaluations')
    parser.add_argument('--grad_clip_norm', type=float, default=-1.,
                        help='grad clip.')
    parser.add_argument('--tfboard', action='store_true', default=False,
                        help='use tensorboard')
    parser.add_argument('--save_folder', default='weights/', type=str, 
                        help='path to save weight')
    parser.add_argument('--vis', dest="vis", action="store_true", default=False,
                        help="visualize input data.")

    # input image size               
    parser.add_argument('--train_min_size', type=int, default=800,
                        help='The shorter train size of the input image')
    parser.add_argument('--train_max_size', type=int, default=1333,
                        help='The longer train size of the input image')
    parser.add_argument('--val_min_size', type=int, default=800,
                        help='The shorter val size of the input image')
    parser.add_argument('--val_max_size', type=int, default=1333,
                        help='The longer val size of the input image')

    # model
    parser.add_argument('-v', '--version', default='yolof50', choices=['yolof18', 'yolof50', 'yolof50-DC5', \
                                                                       'yolof101', 'yolof101-DC5', 'yolof50-DC5-640'],
                        help='build yolof')
    parser.add_argument('--conf_thresh', default=0.05, type=float,
                        help='NMS threshold')
    parser.add_argument('--nms_thresh', default=0.6, type=float,
                        help='NMS threshold')
    parser.add_argument('--topk', default=1000, type=int,
                        help='NMS threshold')
    parser.add_argument('-p', '--coco_pretrained', default=None, type=str,
                        help='coco pretrained weight')

    # dataset
    parser.add_argument('--root', default='/mnt/share/ssd2/dataset',
                        help='data root')
    parser.add_argument('-d', '--dataset', default='coco',
                        help='coco, voc, widerface, crowdhuman')
    
    # Loss
    parser.add_argument('--alpha', default=0.25, type=float,
                        help='focal loss alpha')
    parser.add_argument('--gamma', default=2.0, type=float,
                        help='focal loss gamma')
    parser.add_argument('--loss_cls_weight', default=1.0, type=float,
                        help='weight of cls loss')
    parser.add_argument('--loss_reg_weight', default=1.0, type=float,
                        help='weight of reg loss')
    
    # train trick
    parser.add_argument('--mosaic', action='store_true', default=False,
                        help='Mosaic augmentation')
    parser.add_argument('--no_warmup', action='store_true', default=False,
                        help='do not use warmup')

    # DDP train
    parser.add_argument('-dist', '--distributed', action='store_true', default=False,
                        help='distributed training')
    parser.add_argument('--dist_url', default='env://', 
                        help='url used to set up distributed training')
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--sybn', action='store_true', default=False, 
                        help='use sybn.')

    return parser.parse_args()


def train():
    args = parse_args()
    print("Setting Arguments.. : ", args)
    print("----------------------------------------------------------")

    # dist
    if args.distributed:
        distributed_utils.init_distributed_mode(args)
        print("git:\n  {}\n".format(distributed_utils.get_sha()))

    # path to save model
    path_to_save = os.path.join(args.save_folder, args.dataset, args.version)
    os.makedirs(path_to_save, exist_ok=True)

    # cuda
    if args.cuda:
        print('use cuda')
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")

    # YOLOF Config
    cfg = yolof_config[args.version]
    print('==============================')
    print('Model Configuration: \n', cfg)

    # dataset and evaluator
    dataset, evaluator, num_classes = build_dataset(cfg, args, device)

    # dataloader
    dataloader = build_dataloader(args, dataset, CollateFunc())

    # criterion
    criterion = build_criterion(args=args, device=device, cfg=cfg, num_classes=num_classes)
    
    # build model
    model = build_model(
        args=args, 
        cfg=cfg,
        device=device, 
        num_classes=num_classes, 
        trainable=True
        )
    model = model.to(device).train()

    # DDP
    model_without_ddp = model
    if args.distributed:
        model = DDP(model, device_ids=[args.gpu])
        model_without_ddp = model.module

    # compute FLOPs and Params
    if distributed_utils.is_main_process:
        model_copy = deepcopy(model_without_ddp)
        FLOPs_and_Params(model=model_copy, 
                         min_size=args.train_min_size, 
                         max_size=args.train_max_size, 
                         device=device)
        del model_copy

    if args.distributed:
        # wait for all processes to synchronize
        dist.barrier()

    # optimizer
    base_lr = cfg['base_lr'] * args.batch_size * distributed_utils.get_world_size()
    backbone_lr = base_lr * cfg['bk_lr_ratio']
    optimizer = build_optimizer(model=model_without_ddp,
                                base_lr=base_lr,
                                backbone_lr=backbone_lr,
                                name=cfg['optimizer'],
                                momentum=cfg['momentum'],
                                weight_decay=cfg['weight_decay'])
    
    # lr scheduler
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer=optimizer, 
                                                        milestones=cfg['epoch'][args.schedule]['lr_epoch'])

    # warmup scheduler
    warmup_scheduler = build_warmup(name=cfg['warmup'],
                                    base_lr=base_lr,
                                    wp_iter=cfg['wp_iter'],
                                    warmup_factor=cfg['warmup_factor'])

    # training configuration
    max_epoch = cfg['epoch'][args.schedule]['max_epoch']
    epoch_size = len(dataloader)
    best_map = -1.
    warmup = not args.no_warmup

    t0 = time.time()
    # start training loop
    for epoch in range(max_epoch):
        if args.distributed:
            dataloader.sampler.set_epoch(epoch)            

        # train one epoch
        for iter_i, (images, targets, masks) in enumerate(dataloader):
            ni = iter_i + epoch * epoch_size
            # warmup
            if ni < cfg['wp_iter'] and warmup:
                warmup_scheduler.warmup(ni, optimizer)

            elif ni == cfg['wp_iter'] and warmup:
                # warmup is over
                print('Warmup is over')
                warmup = False
                warmup_scheduler.set_lr(optimizer, base_lr, base_lr)

            # to device
            images = images.to(device)
            masks = masks.to(device)
            targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

            # visualize input data
            if args.vis:
                vis_data(images, targets, masks)
                continue

            # inference
            outputs = model(images, mask=masks)

            # compute loss
            loss_dict = criterion(outputs, targets)
            losses = loss_dict['total_loss']
            
            loss_dict_reduced = distributed_utils.reduce_dict(loss_dict)

            # check loss
            if torch.isnan(losses):
                print('loss is NAN !!')
                continue

            # Backward and Optimize
            losses.backward()
            if args.grad_clip_norm > 0.:
                total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip_norm)
            else:
                total_norm = get_total_grad_norm(model.parameters())
            optimizer.step()
            optimizer.zero_grad()

            # display
            if distributed_utils.is_main_process() and iter_i % 10 == 0:
                t1 = time.time()
                cur_lr = [param_group['lr']  for param_group in optimizer.param_groups]
                cur_lr_dict = {'lr': cur_lr[0], 'lr_bk': cur_lr[1]}
                # basic infor
                log =  '[Epoch: {}/{}]'.format(epoch+1, max_epoch)
                log += '[Iter: {}/{}]'.format(iter_i, epoch_size)
                log += '[lr: {:.6f}][lr_bk: {:.6f}]'.format(cur_lr_dict['lr'], cur_lr_dict['lr_bk'])
                # loss infor
                for k in loss_dict_reduced.keys():
                    log += '[{}: {:.2f}]'.format(k, loss_dict_reduced[k])

                # other infor
                log += '[time: {:.2f}]'.format(t1 - t0)
                log += '[gnorm: {:.2f}]'.format(total_norm)
                log += '[size: [{}, {}]]'.format(args.train_min_size, args.train_max_size)

                # print log infor
                print(log, flush=True)
                
                t0 = time.time()

        lr_scheduler.step()
        
        # evaluation
        if epoch % args.eval_epoch == 0 or (epoch + 1) == max_epoch:
            # check evaluator
            if distributed_utils.is_main_process():
                if evaluator is None:
                    print('No evaluator ... save model and go on training.')
                    print('Saving state, epoch: {}'.format(epoch + 1))
                    weight_name = '{}_epoch_{}.pth'.format(args.version, epoch + 1)
                    checkpoint_path = os.path.join(path_to_save, weight_name)
                    torch.save({'model': model_without_ddp.state_dict(),
                                'epoch': epoch,
                                'args': args}, 
                                checkpoint_path)                      
                else:
                    print('eval ...')
                    model_eval = model_without_ddp

                    # set eval mode
                    model_eval.trainable = False
                    model_eval.eval()

                    # evaluate
                    evaluator.evaluate(model_eval)

                    cur_map = evaluator.map
                    if cur_map > best_map:
                        # update best-map
                        best_map = cur_map
                        # save model
                        print('Saving state, epoch:', epoch + 1)
                        weight_name = '{}_epoch_{}_{:.2f}.pth'.format(args.version, epoch + 1, best_map*100)
                        checkpoint_path = os.path.join(path_to_save, weight_name)
                        torch.save({'model': model_without_ddp.state_dict(),
                                    'epoch': epoch,
                                    'args': args}, 
                                    checkpoint_path)                      

                    # set train mode.
                    model_eval.trainable = True
                    model_eval.train()
        
            if args.distributed:
                # wait for all processes to synchronize
                dist.barrier()

        # close mosaic augmentation
        if args.mosaic and max_epoch - epoch == 5:
            print('close Mosaic Augmentation ...')
            dataloader.dataset.mosaic = False



if __name__ == '__main__':
    train()
