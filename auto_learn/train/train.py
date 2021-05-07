import sys
import os
import argparse
import random
import shutil
import time
import warnings
import json
from collections import OrderedDict

BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(BASE_DIR)
sys.path.append("../")
warnings.filterwarnings('ignore')

import numpy as np
from thop import profile
from thop import clever_format
from tqdm import tqdm
import apex
from apex import amp
from apex.parallel import convert_syncbn_model
from apex.parallel import DistributedDataParallel
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.distributed as dist
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from torchvision import transforms
from configs.config import Config
from public.detection.dataset.cocodataset import COCODataPrefetcher, Collater
from public.detection.models.loss import FCOSLoss
from public.detection.models.decode import FCOSDecoder
from public.detection.models import yolof2 as yolof
from public.imagenet.utils import get_logger
from pycocotools.cocoeval import COCOeval


def parse_args():
    parser = argparse.ArgumentParser(
        description='PyTorch COCO Detection Distributed Training')
    parser.add_argument('--network',
                        type=str,
                        default=Config.network,
                        help='name of network')
    parser.add_argument('--lr',
                        type=float,
                        default=Config.lr,
                        help='learning rate')
    parser.add_argument('--epochs',
                        type=int,
                        default=Config.epochs,
                        help='num of training epochs')
    parser.add_argument('--per_node_batch_size',
                        type=int,
                        default=Config.per_node_batch_size,
                        help='per_node batch size')
    parser.add_argument('--pretrained',
                        type=bool,
                        default=Config.pretrained,
                        help='load pretrained model params or not')
    parser.add_argument('--num_classes',
                        type=int,
                        default=Config.num_classes,
                        help='model classification num')
    parser.add_argument('--input_image_size',
                        type=int,
                        default=Config.input_image_size,
                        help='input image size')
    parser.add_argument('--num_workers',
                        type=int,
                        default=Config.num_workers,
                        help='number of worker to load data')
    parser.add_argument('--resume',
                        type=str,
                        default=Config.resume,
                        help='put the path to resuming file if needed')
    parser.add_argument('--checkpoints',
                        type=str,
                        default=Config.checkpoint_path,
                        help='path for saving trained models')
    parser.add_argument('--log',
                        type=str,
                        default=Config.log,
                        help='path to save log')
    parser.add_argument('--evaluate',
                        type=str,
                        default=Config.evaluate,
                        help='path for evaluate model')
    parser.add_argument('--seed', type=int, default=Config.seed, help='seed')
    parser.add_argument('--print_interval',
                        type=bool,
                        default=Config.print_interval,
                        help='print interval')
    parser.add_argument('--apex',
                        type=bool,
                        default=Config.apex,
                        help='use apex or not')
    parser.add_argument('--sync_bn',
                        type=bool,
                        default=Config.sync_bn,
                        help='use sync bn or not')
    parser.add_argument('--local_rank',
                        type=int,
                        default=0,
                        help='LOCAL_PROCESS_RANK')

    parser.add_argument('--use_gn',
                        type=bool,
                        default=Config.use_gn,
                        help='LOCAL_PROCESS_RANK')
    parser.add_argument('--version',
                        type=int,
                        default=Config.version,
                        help='current version')


    return parser.parse_args()


def validate(val_dataset, model, decoder, args):
    if args.apex:
        model = model.module
    # switch to evaluate mode
    model.eval()
    with torch.no_grad():
        all_eval_result = evaluate_coco(val_dataset, model, decoder, args)

    return all_eval_result


def evaluate_coco(val_dataset, model, decoder, args):
    results, image_ids = [], []
    indexes = []
    for index in range(len(val_dataset)):
        indexes.append(index)

    batch_size = args.per_node_batch_size
    eval_collater = Collater()
    val_loader = DataLoader(val_dataset,
                            batch_size=batch_size,
                            shuffle=False,
                            num_workers=args.num_workers,
                            collate_fn=eval_collater.next)

    start_time = time.time()

    for i, data in tqdm(enumerate(val_loader)):
        images, scales = torch.tensor(data['img']), torch.tensor(data['scale'])
        per_batch_indexes = indexes[i * batch_size:(i + 1) * batch_size]

        images = images.cuda().float()
        cls_heads, reg_heads, center_heads, batch_positions = model(images)
        scores, classes, boxes = decoder(cls_heads, reg_heads, center_heads,
                                         batch_positions)

        scores, classes, boxes = scores.cpu(), classes.cpu(), boxes.cpu()
        scales = scales.unsqueeze(-1).unsqueeze(-1)
        boxes /= scales

        for per_image_scores, per_image_classes, per_image_boxes, index in zip(
                scores, classes, boxes, per_batch_indexes):
            # for coco_eval,we need [x_min,y_min,w,h] format pred boxes
            per_image_boxes[:, 2:] -= per_image_boxes[:, :2]

            for object_score, object_class, object_box in zip(
                    per_image_scores, per_image_classes, per_image_boxes):
                object_score = float(object_score)
                object_class = int(object_class)
                object_box = object_box.tolist()
                if object_class == -1:
                    break

                image_result = {
                    'image_id':
                    val_dataset.image_ids[index],
                    'category_id':
                    val_dataset.find_category_id_from_coco_label(object_class),
                    'score':
                    object_score,
                    'bbox':
                    object_box,
                }
                results.append(image_result)

            image_ids.append(val_dataset.image_ids[index])

            print('{}/{}'.format(index, len(val_dataset)), end='\r')

    testing_time = (time.time() - start_time)
    per_image_testing_time = testing_time / len(val_dataset)

    print(f"per_image_testing_time:{per_image_testing_time:.3f}")

    if not len(results):
        print(f"No target detected in test set images")
        return

    json.dump(results,
              open('{}_bbox_results.json'.format(val_dataset.set_name), 'w'),
              indent=4)

    # load results in COCO evaluation tool
    coco_true = val_dataset.coco
    coco_pred = coco_true.loadRes('{}_bbox_results.json'.format(
        val_dataset.set_name))

    coco_eval = COCOeval(coco_true, coco_pred, 'bbox')
    coco_eval.params.imgIds = image_ids
    coco_eval.evaluate()
    coco_eval.accumulate()
    coco_eval.summarize()
    all_eval_result = coco_eval.stats

    return all_eval_result


def main():
    args = parse_args()
    global local_rank
    local_rank = args.local_rank
    if local_rank == 0:
        global logger
        logger = get_logger(__name__, args.log)

    torch.cuda.empty_cache()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        cudnn.deterministic = True

    torch.cuda.set_device(local_rank)
    dist.init_process_group(backend='nccl', init_method='env://')

    global gpus_num
    gpus_num = torch.cuda.device_count()
    if local_rank == 0:
        logger.info(f'use {gpus_num} gpus')
        logger.info(f"args: {args}")

    cudnn.benchmark = True
    cudnn.enabled = True
    start_time = time.time()

    # dataset and dataloader
    if local_rank == 0:
        logger.info('start loading data')
    train_sampler = torch.utils.data.distributed.DistributedSampler(
        Config.train_dataset, shuffle=True)
    collater = Collater()
    train_loader = DataLoader(Config.train_dataset,
                              batch_size=args.per_node_batch_size,
                              shuffle=False,
                              num_workers=args.num_workers,
                              collate_fn=collater.next,
                              sampler=train_sampler)
    if local_rank == 0:
        logger.info('finish loading data')

    model = yolof.__dict__[args.network](**{
        "pretrained": args.pretrained,
        "num_classes": args.num_classes,
        "use_gn": args.use_gn,
    })

    
    if args.version == 1:
        pre_model = torch.load('/home/jovyan/data-vol-polefs-1/yolof_dc5_res50_coco667_withImPre/best.pth', map_location='cpu')
    else:
        pre_model = torch.load('/home/jovyan/data-vol-polefs-1/small_sample/checkpoints/v{}/best.pth'.format(args.version-1), map_location='cpu')
    
    def copyStateDict(state_dict):
        if list(state_dict.keys())[0].startswith('module'):
            start_idx = 1
        else:
            start_idx = 0
        new_state_dict = OrderedDict()
        for k,v in state_dict.items():
            name = '.'.join(k.split('.')[start_idx:])

            new_state_dict[name] = v
        return new_state_dict
    new_dict=copyStateDict(pre_model)

    keys=[]
    for k,v in new_dict.items():
        if k.startswith('clsregcnt_head.cls_out'):    #将‘clsregcnt_head’开头的key过滤掉，这里是要去除的层的key
            continue
        if args.version != 1:
            if k.startswith('clsregcnt_head.reg_out'):
                continue
            if k.startswith('clsregcnt_head.center_out'):
                continue
        keys.append(k)
    model.load_state_dict({k:new_dict[k] for k in keys}, strict = False)
#     model.load_state_dict(pre_model, strict=False)
    
    model.scales.requires_grad = False
    
    for p in model.backbone.parameters():
        p.requires_grad = False
    for p in model.dila_encoder.parameters():
            p.requires_grad = False
    for p in model.trans.parameters():
            p.requires_grad = False
    for p in model.c4_out.parameters():
            p.requires_grad = False
    
    for name, param in model.named_parameters():
        if local_rank == 0:
            logger.info(f"{name},{param.requires_grad}")

    flops_input = torch.randn(1, 3, args.input_image_size,
                              args.input_image_size)
    flops, params = profile(model, inputs=(flops_input, ))
    flops, params = clever_format([flops, params], "%.3f")
    if local_rank == 0:
        logger.info(
            f"model: '{args.network}', flops: {flops}, params: {params}")

    criterion = FCOSLoss(strides=[16],
                 mi=[[0, 512]]).cuda()
    decoder = FCOSDecoder(image_w=args.input_image_size,
                          image_h=args.input_image_size, strides=[16]).cuda()

    model = model.cuda()
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer,
                                                           patience=3,
                                                           verbose=True)
#     scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer=optimizer, T_max=4, eta_min=1e-6, last_epoch=-1)

    if args.sync_bn:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    if args.apex:
        amp.register_float_function(torch, 'sigmoid')
        amp.register_float_function(torch, 'softmax')
        model, optimizer = amp.initialize(model, optimizer, opt_level='O1')
        model = apex.parallel.DistributedDataParallel(model,
                                                      delay_allreduce=True)
        if args.sync_bn:
            model = apex.parallel.convert_syncbn_model(model)
#     else:
#         model = nn.parallel.DistributedDataParallel(model,
#                                                     device_ids=[local_rank],
#                                                     output_device=local_rank)

    if args.evaluate:
        if not os.path.isfile(args.evaluate):
            if local_rank == 0:
                logger.exception(
                    '{} is not a file, please check it again'.format(
                        args.resume))
            sys.exit(-1)
        if local_rank == 0:
            logger.info('start only evaluating')
            logger.info(f"start resuming model from {args.evaluate}")
        checkpoint = torch.load(args.evaluate,
                                map_location=torch.device('cpu'))
        model.load_state_dict(checkpoint['model_state_dict'])
        if local_rank == 0:
            logger.info(f"start eval.")
            all_eval_result = validate(Config.val_dataset, model, decoder,
                                       args)
            logger.info(f"eval done.")
            if all_eval_result is not None:
                logger.info(
                    f"val: epoch: {checkpoint['epoch']:0>5d}, IoU=0.5:0.95,area=all,maxDets=100,mAP:{all_eval_result[0]:.3f}, IoU=0.5,area=all,maxDets=100,mAP:{all_eval_result[1]:.3f}, IoU=0.75,area=all,maxDets=100,mAP:{all_eval_result[2]:.3f}, IoU=0.5:0.95,area=small,maxDets=100,mAP:{all_eval_result[3]:.3f}, IoU=0.5:0.95,area=medium,maxDets=100,mAP:{all_eval_result[4]:.3f}, IoU=0.5:0.95,area=large,maxDets=100,mAP:{all_eval_result[5]:.3f}, IoU=0.5:0.95,area=all,maxDets=1,mAR:{all_eval_result[6]:.3f}, IoU=0.5:0.95,area=all,maxDets=10,mAR:{all_eval_result[7]:.3f}, IoU=0.5:0.95,area=all,maxDets=100,mAR:{all_eval_result[8]:.3f}, IoU=0.5:0.95,area=small,maxDets=100,mAR:{all_eval_result[9]:.3f}, IoU=0.5:0.95,area=medium,maxDets=100,mAR:{all_eval_result[10]:.3f}, IoU=0.5:0.95,area=large,maxDets=100,mAR:{all_eval_result[11]:.3f}"
                )

        return

    best_map = 0.0
    start_epoch = 1
    # resume training
    if os.path.exists(args.resume):
        if local_rank == 0:
            logger.info(f"start resuming model from {args.resume}")
        checkpoint = torch.load(args.resume, map_location=torch.device('cpu'))
        start_epoch += checkpoint['epoch']
        best_map = checkpoint['best_map']
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        if local_rank == 0:
            logger.info(
                f"finish resuming model from {args.resume}, epoch {checkpoint['epoch']}, best_map: {checkpoint['best_map']}, "
                f"loss: {checkpoint['loss']:3f}, cls_loss: {checkpoint['cls_loss']:2f}, reg_loss: {checkpoint['reg_loss']:2f}, center_ness_loss: {checkpoint['center_ness_loss']:2f}"
            )

    if local_rank == 0:
        if not os.path.exists(args.checkpoints):
            os.makedirs(args.checkpoints)

    if local_rank == 0:
        logger.info('start training')
    for epoch in range(start_epoch, args.epochs + 1):
        train_sampler.set_epoch(epoch)
        cls_losses, reg_losses, center_ness_losses, losses = train(
            train_loader, model, criterion, optimizer, scheduler, epoch, args)

        if local_rank == 0:
            logger.info(
                f"train: epoch {epoch:0>3d}, cls_loss: {cls_losses:.2f}, reg_loss: {reg_losses:.2f}, center_ness_loss: {center_ness_losses:.2f}, loss: {losses:.2f}"
            )

        if epoch % 12 == 0 or epoch % 24 == 0 or epoch == args.epochs:
            if local_rank == 0:
                logger.info(f"start eval.")
                all_eval_result = validate(Config.val_dataset, model, decoder,
                                           args)
                logger.info(f"eval done.")
                if all_eval_result is not None:
                    logger.info(
                        f"val: epoch: {epoch:0>5d}, IoU=0.5:0.95,area=all,maxDets=100,mAP:{all_eval_result[0]:.3f}, IoU=0.5,area=all,maxDets=100,mAP:{all_eval_result[1]:.3f}, IoU=0.75,area=all,maxDets=100,mAP:{all_eval_result[2]:.3f}, IoU=0.5:0.95,area=small,maxDets=100,mAP:{all_eval_result[3]:.3f}, IoU=0.5:0.95,area=medium,maxDets=100,mAP:{all_eval_result[4]:.3f}, IoU=0.5:0.95,area=large,maxDets=100,mAP:{all_eval_result[5]:.3f}, IoU=0.5:0.95,area=all,maxDets=1,mAR:{all_eval_result[6]:.3f}, IoU=0.5:0.95,area=all,maxDets=10,mAR:{all_eval_result[7]:.3f}, IoU=0.5:0.95,area=all,maxDets=100,mAR:{all_eval_result[8]:.3f}, IoU=0.5:0.95,area=small,maxDets=100,mAR:{all_eval_result[9]:.3f}, IoU=0.5:0.95,area=medium,maxDets=100,mAR:{all_eval_result[10]:.3f}, IoU=0.5:0.95,area=large,maxDets=100,mAR:{all_eval_result[11]:.3f}"
                    )
                    if all_eval_result[0] > best_map:
                        torch.save(model.module.state_dict(),
                                   os.path.join(args.checkpoints, "best.pth"))
                        best_map = all_eval_result[0]
        if local_rank == 0:
            torch.save(
                {
                    'epoch': epoch,
                    'best_map': best_map,
                    'cls_loss': cls_losses,
                    'reg_loss': reg_losses,
                    'center_ness_loss': center_ness_losses,
                    'loss': losses,
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'scheduler_state_dict': scheduler.state_dict(),
                }, os.path.join(args.checkpoints, 'latest.pth'))

    if local_rank == 0:
        logger.info(f"finish training, best_map: {best_map:.3f}")
    training_time = (time.time() - start_time) / 3600
    if local_rank == 0:
        logger.info(
            f"finish training, total training time: {training_time:.2f} hours")
    
    if local_rank == 0:
        os.system("python ../find_new.py {}".format(args.version + 1))


def train(train_loader, model, criterion, optimizer, scheduler, epoch, args):
    cls_losses, reg_losses, center_ness_losses, losses = [], [], [], []

    # switch to train mode
    model.train()
#     for p in model.module.backbone.parameters():
#         p.requires_grad = False
#     for p in model.module.dila_encoder.parameters():
#             p.requires_grad = False
#     for p in model.module.trans.parameters():
#             p.requires_grad = False
#     for p in model.module.c4_out.parameters():
#             p.requires_grad = False

    iters = len(train_loader.dataset) // (args.per_node_batch_size * gpus_num)
    prefetcher = COCODataPrefetcher(train_loader)
    images, annotations = prefetcher.next()
    iter_index = 1

    while images is not None:
        images, annotations = images.cuda().float(), annotations.cuda()
        cls_heads, reg_heads, center_heads, batch_positions = model(images)
        cls_loss, reg_loss, center_ness_loss = criterion(
            cls_heads, reg_heads, center_heads, batch_positions, annotations)

        loss = cls_loss + reg_loss + center_ness_loss
        if cls_loss == 0.0 or reg_loss == 0.0:
            optimizer.zero_grad()
            print("zero")
            continue
#*********************************************************************************************************
        if args.apex:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
#         loss.backward()

        torch.nn.utils.clip_grad_norm_(model.parameters(), 0.1)
        optimizer.step()
        optimizer.zero_grad()

        cls_losses.append(cls_loss.item())
        reg_losses.append(reg_loss.item())
        center_ness_losses.append(center_ness_loss.item())
        losses.append(loss.item())
        

        images, annotations = prefetcher.next()

        if local_rank == 0 and iter_index % args.print_interval == 0:
            logger.info(
                f"train: epoch {epoch:0>3d}, iter [{iter_index:0>5d}, {iters:0>5d}], cls_loss: {cls_loss.item():.2f}, reg_loss: {reg_loss.item():.2f}, center_ness_loss: {center_ness_loss.item():.2f}, loss_total: {loss.item():.2f}"
            )

        iter_index += 1

#     scheduler.step(np.mean(losses))

    return np.mean(cls_losses), np.mean(reg_losses), np.mean(
        center_ness_losses), np.mean(losses)


if __name__ == '__main__':
    main()
   