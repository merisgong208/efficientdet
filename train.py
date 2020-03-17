import os
import argparse
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision import transforms
from src.dataset import CocoDataset, Resizer, Normalizer, Augmenter, collater
from src.model import EfficientDet
from tensorboardX import SummaryWriter
import shutil
import numpy as np
from tqdm.autonotebook import tqdm
from datetime import datetime
os.environ['CUDA_VISIBLE_DEVICES']='3,4,5,6'
def get_args():
    parser = argparse.ArgumentParser(
        "EfficientDet: Scalable and Efficient Object Detection implementation by Signatrix GmbH")
    parser.add_argument("--image_size", type=int, default=512, help="The common width and height for all images")
    parser.add_argument("--batch_size", type=int, default=3, help="The number of images per batch")
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument('--alpha', type=float, default=0.25)
    parser.add_argument('--gamma', type=float, default=1.5)
    parser.add_argument("--num_epochs", type=int, default=200)
    parser.add_argument("--test_interval", type=int, default=1, help="Number of epoches between testing phases")
    parser.add_argument("--es_min_delta", type=float, default=0.0,
                        help="Early stopping's parameter: minimum change loss to qualify as an improvement")
    parser.add_argument("--es_patience", type=int, default=0,
                        help="Early stopping's parameter: number of epochs with no improvement after which training will be stopped. Set to 0 to disable this technique.")
    parser.add_argument("--data_path", type=str, default="data/coco", help="the root folder of dataset")
    parser.add_argument("--log_path", type=str, default="tensorboard/signatrix_efficientdet_coco")
    parser.add_argument("--saved_path", type=str, default="trained_models")

    
    parser.add_argument("--save_interval", type=int, default=1, help="Number of epoches between two operations for saving weights")
    parser.add_argument('--backbone_network', default='efficientnet-b7', type=str,
                    help='efficientdet-[b0, b1, ..]')
    parser.add_argument('--remote_loading', default=False, type=bool,
                    help='if this option is enabled, it will download and load the backbone weights from https://github.com/lukemelas/EfficientNet-PyTorch/releases,'+ \
                          'otherwise,load the weights locally from ./pretrained_models')
    parser.add_argument('--advprop', default=False, type=bool,
                    help='if this option is enabled, the adv_efficientnet_b* backbone will be used instead of efficientnet_b*')
    parser.add_argument('--resume', action='store_true',
                    help='If resume training from the last model file saved by the last stopped training')
    parser.add_argument('--start_epoch', default=0, type=int,
                    help='The start_epoch where you restart training by resuming from a model generated recently')

    args = parser.parse_args()
    return args


def train(opt):
    num_gpus = 1
    if torch.cuda.is_available():
        num_gpus = torch.cuda.device_count()
        torch.cuda.manual_seed(123)
    else:
        torch.manual_seed(123)

    training_params = {"batch_size": opt.batch_size * num_gpus,
                       "shuffle": True,
                       "drop_last": True,
                       "collate_fn": collater,
                       "num_workers": 12}

    test_params = {"batch_size": opt.batch_size,
                   "shuffle": False,
                   "drop_last": False,
                   "collate_fn": collater,
                   "num_workers": 12}

    training_set = CocoDataset(root_dir=opt.data_path, set="train2017",
                               transform=transforms.Compose([Normalizer(), Augmenter(), Resizer()]))
    training_generator = DataLoader(training_set, **training_params)

    test_set = CocoDataset(root_dir=opt.data_path, set="val2017",
                           transform=transforms.Compose([Normalizer(), Resizer()]))
    test_generator = DataLoader(test_set, **test_params)
    
    channels_map={
       'efficientnet-b0': [40,80,192],
       'efficientnet-b1': [40,80,192],
       'efficientnet-b2': [48,88,208],
       'efficientnet-b3': [48,96,232],
       'efficientnet-b4': [56,112,272],
       'efficientnet-b5': [64,128,304],
       'efficientnet-b6': [72,144,344],
       'efficientnet-b7': [80,160,384],
    }


    if os.path.isdir(opt.log_path):
        shutil.rmtree(opt.log_path)
    os.makedirs(opt.log_path)

    if not os.path.isdir(opt.saved_path):
        os.makedirs(opt.saved_path)

    writer = SummaryWriter(opt.log_path)

    if opt.resume:
        resume_path = os.path.join(opt.saved_path,'signatrix_efficientdet_coco_latest.pth')
        model = torch.load(resume_path).module
        print("model loaded from {}".format(resume_path))
    else:
        model = EfficientDet(num_classes=training_set.num_classes(),network=opt.backbone_network,remote_loading=opt.remote_loading,advprop=opt.advprop,conv_in_channels=channels_map[opt.backbone_network] )
        print("model created with backbone {}, advprop {}".format(opt.backbone_network,opt.advprop))

    if torch.cuda.is_available():
        model = model.cuda()
        model = nn.DataParallel(model)

    optimizer = torch.optim.Adam(model.parameters(), opt.lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=3, verbose=True)

    best_loss = 1e5
    best_epoch = 0
    model.train()

    num_iter_per_epoch = len(training_generator)
    
    start_epoch=0
    if opt.resume:
        start_epoch = opt.start_epoch
    for epoch in range(start_epoch,opt.num_epochs):
        model.train()
        # if torch.cuda.is_available():
        #     model.module.freeze_bn()
        # else:
        #     model.freeze_bn()
        epoch_loss = []
        progress_bar = tqdm(training_generator)
        for iter, data in enumerate(progress_bar):
            try:
                optimizer.zero_grad()
                if torch.cuda.is_available():
                    cls_loss, reg_loss = model([data['img'].cuda().float(), data['annot'].cuda()])
                else:
                    cls_loss, reg_loss = model([data['img'].float(), data['annot']])

                cls_loss = cls_loss.mean()
                reg_loss = reg_loss.mean()
                loss = cls_loss + reg_loss
                if loss == 0:
                    continue
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 0.1)
                optimizer.step()
                epoch_loss.append(float(loss))
                total_loss = np.mean(epoch_loss)

                progress_bar.set_description(
                    '{} Epoch: {}/{}. Iteration: {}/{}. Cls loss: {:.5f}. Reg loss: {:.5f}. Batch loss: {:.5f} Total loss: {:.5f}'.format(
                        datetime.now(),epoch + 1, opt.num_epochs, iter + 1, num_iter_per_epoch, cls_loss, reg_loss, loss,
                        total_loss))
                writer.add_scalar('Train/Total_loss', total_loss, epoch * num_iter_per_epoch + iter)
                writer.add_scalar('Train/Regression_loss', reg_loss, epoch * num_iter_per_epoch + iter)
                writer.add_scalar('Train/Classfication_loss (focal loss)', cls_loss, epoch * num_iter_per_epoch + iter)

            except Exception as e:
                print(e)
                continue
        scheduler.step(np.mean(epoch_loss))

        if epoch % opt.test_interval == 0:
            model.eval()
            loss_regression_ls = []
            loss_classification_ls = []
            for iter, data in enumerate(test_generator):
                with torch.no_grad():
                    if torch.cuda.is_available():
                        cls_loss, reg_loss = model([data['img'].cuda().float(), data['annot'].cuda()])
                    else:
                        cls_loss, reg_loss = model([data['img'].float(), data['annot']])

                    cls_loss = cls_loss.mean()
                    reg_loss = reg_loss.mean()

                    loss_classification_ls.append(float(cls_loss))
                    loss_regression_ls.append(float(reg_loss))

            cls_loss = np.mean(loss_classification_ls)
            reg_loss = np.mean(loss_regression_ls)
            loss = cls_loss + reg_loss

            print(
                '{} Epoch: {}/{}. Classification loss: {:1.5f}. Regression loss: {:1.5f}. Total loss: {:1.5f}'.format(
                   datetime.now(), epoch + 1, opt.num_epochs, cls_loss, reg_loss,
                    np.mean(loss)))
            writer.add_scalar('Test/Total_loss', loss, epoch)
            writer.add_scalar('Test/Regression_loss', reg_loss, epoch)
            writer.add_scalar('Test/Classfication_loss (focal loss)', cls_loss, epoch)

            if loss + opt.es_min_delta < best_loss:
                best_loss = loss
                best_epoch = epoch
                torch.save(model, os.path.join(opt.saved_path, "signatrix_efficientdet_coco_best_epoch{}.pth".format(epoch)))
                ''' 
                dummy_input = torch.rand(opt.batch_size, 3, 512, 512)
                if torch.cuda.is_available():
                    dummy_input = dummy_input.cuda()
                if isinstance(model, nn.DataParallel):
                    model.module.backbone_net.model.set_swish(memory_efficient=False)
                    
                    torch.onnx.export(model.module, dummy_input,
                                      os.path.join(opt.saved_path, "signatrix_efficientdet_coco.onnx"),
                                      verbose=False)
                    
                    model.module.backbone_net.model.set_swish(memory_efficient=True)
                else:
                    model.backbone_net.model.set_swish(memory_efficient=False)
                    
                    torch.onnx.export(model, dummy_input,
                                      os.path.join(opt.saved_path, "signatrix_efficientdet_coco.onnx"),
                                      verbose=False)
                    
                    model.backbone_net.model.set_swish(memory_efficient=True)
                '''
            print("epoch:",epoch,"best_epoch:",best_epoch,"epoch - best_epoch=", epoch - best_epoch)
            # Early stopping
            if epoch - best_epoch > opt.es_patience > 0:
                print("Stop training at epoch {}. The lowest loss achieved is {}".format(epoch, loss))
                break
        if epoch % opt.save_interval ==0:
            torch.save(model, os.path.join(opt.saved_path, "signatrix_efficientdet_coco_latest.pth"))
    writer.close()


if __name__ == "__main__":
    opt = get_args()
    train(opt)
