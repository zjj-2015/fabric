import sys, glob, cv2, random, math, argparse
import numpy as np
import pandas as pd
from tqdm import trange
from sklearn.metrics import precision_recall_fscore_support as prfs

import torch
import torch.utils.data
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.autograd as autograd
from torch.autograd import Variable
from torchvision import datasets, models, transforms

sys.path.append('.')
from utils.dataloaders import *
from models.bidate_model import *
from utils.metrics import *

from polyaxon_client.tracking import Experiment, get_log_level, get_data_paths, get_outputs_path
from polystores.stores.manager import StoreManager

# from moonshot import alert


import logging

logging.basicConfig(level=logging.INFO)


def get_weight_filename(weight_file):
    return '{}/{}'.format(get_outputs_path(), 'checkpoint.pth.tar')



parser = argparse.ArgumentParser(description='Training change detection network')

parser.add_argument('--patch_size', type=int, default=120, required=False, help='input patch size')
parser.add_argument('--stride', type=int, default=10, required=False, help='stride at which to sample patches')
parser.add_argument('--aug', default=True, required=False, help='Do augmentation or not')

parser.add_argument('--gpu_ids', default='0,1,2,3', required=False, help='gpus ids for parallel training')
parser.add_argument('--num_workers', type=int, default=90, required=False, help='Number of cpu workers')

parser.add_argument('--epochs', type=int, default=10, required=False, help='number of eochs to train')
parser.add_argument('--batch_size', type=int, default=256, required=False, help='batch size for training')
parser.add_argument('--lr', type=float, default=0.01, required=False, help='Learning rate')

parser.add_argument('--loss', type=str, default='bce', required=False, help='bce,focal,dice,jaccard,tversky')
parser.add_argument('--gamma', type=float, default=2, required=False, help='if focal loss is used pass gamma')
parser.add_argument('--alpha', type=float, default=0.5, required=False, help='if tversky loss is used pass alpha')
parser.add_argument('--beta', type=float, default=0.5, required=False, help='if tversky loss is used pass beta')

parser.add_argument('--val_cities', default='0,1', required=False, help='''cities to use for validation,
                            0:abudhabi, 1:aguasclaras, 2:beihai, 3:beirut, 4:bercy, 5:bordeaux, 6:cupertino, 7:hongkong, 8:mumbai,
                            9:nantes, 10:paris, 11:pisa, 12:rennes, 14:saclay_e''')

parser.add_argument('--data_dir', default='../datasets/onera/', required=False, help='data directory for training')
parser.add_argument('--weight_dir', default='../weights/', required=False, help='directory to save weights')
parser.add_argument('--weight_file', default='', required=False, help='if defined and available, will preload weights from this file')
parser.add_argument('--log_dir', default='../logs/', required=False, help='directory to save training log')


opt = parser.parse_args()

if opt.loss == 'bce' or opt.loss == 'dice' or opt.loss == 'jaccard':
    path = 'cd_patchSize_' + str(opt.patch_size) + '_stride_' + str(opt.stride) + \
            '_batchSize_' + str(opt.batch_size) + '_loss_' + opt.loss  + \
            '_lr_' + str(opt.lr) + '_epochs_' + str(opt.epochs) +\
            '_valCities_' + opt.val_cities

if opt.loss == 'focal':
    path = 'cd_patchSize_' + str(opt.patch_size) + '_stride_' + str(opt.stride) + \
            '_batchSize_' + str(opt.batch_size) + '_loss_' + opt.loss + '_gamma_' + str(opt.gamma) + \
            '_lr_' + str(opt.lr) + '_epochs_' + str(opt.epochs) +\
            '_valCities_' + opt.val_cities

if opt.loss == 'tversky':
    path = 'cd_patchSize_' + str(opt.patch_size) + '_stride_' + str(opt.stride) + \
            '_batchSize_' + str(opt.batch_size) + '_loss_' + opt.loss + '_alpha_' + str(opt.alpha) + '_beta_' + str(opt.beta) + \
            '_lr_' + str(opt.lr) + '_epochs_' + str(opt.epochs) +\
            '_valCities_' + opt.val_cities

weight_file = opt.weight_file

weight_path = opt.weight_dir + path + '.pt'
log_path = opt.log_dir + path + '.log'

device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
logging.info('GPU AVAILABLE? ' + str(torch.cuda.is_available()))
logging.info('STARTING data download')
data_paths = list(get_data_paths().values())[0]
data_store = StoreManager(path=data_paths)
data_store.download_dir('onera')
experiment = Experiment()

train_samples, test_samples = get_train_val_metadata(opt.data_dir, opt.val_cities, opt.patch_size, opt.stride)
print ('train samples : ', len(train_samples))
print ('test samples : ', len(test_samples))
experiment.log_metrics(epoch=0,
                        train_f1_score=0,
                        train_recall=0,
                        train_prec=0,
                        train_loss=0,
                        train_accuracy=0,
                        test_f1_score=0,
                        test_recall=0,
                        test_prec=0,
                        test_loss=0,
                        test_accuracy=0)


logging.info('STARTING Dataset Creation')

full_load = full_onera_loader(opt.data_dir)

train_dataset = OneraPreloader(opt.data_dir, train_samples, full_load, opt.patch_size, opt.aug)
test_dataset = OneraPreloader(opt.data_dir, test_samples, full_load, opt.patch_size, opt.aug)

logging.info('STARTING Dataloading')

train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=opt.batch_size, shuffle=True, num_workers=opt.num_workers)
test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=opt.batch_size, shuffle=True, num_workers=opt.num_workers)

logging.info('LOADING Model')

model = BiDateNet(13, 2).to(device)
model = nn.DataParallel(model, device_ids=[int(x) for x in opt.gpu_ids.split(',')])
#
# if os.path.exists(opt.weight_dir) and weight_file in os.listdir(opt.weight_dir):
#     model = torch.load(opt.weight_dir + 'cd_patchSize_90_stride_10_batchSize_512_loss_tversky_alpha_0.08_beta_0.92_lr_0.01_epochs_10_valCities_0,1.pt')

if opt.loss == 'bce':
    criterion = nn.BCEWithLogitsLoss()
if opt.loss == 'focal':
    criterion = FocalLoss(opt.gamma)
if opt.loss == 'dice':
    criterion = dice_loss
if opt.loss == 'jaccard':
    criterion = jaccard_loss
if opt.loss == 'tversky':
    criterion = TverskyLoss(alpha=opt.alpha, beta=opt.beta)

optimizer = optim.SGD(model.parameters(), lr=opt.lr)


best_f1s = -1
best_metric = {}

logging.info('STARTING training')

for epoch in range(opt.epochs):

    train_losses = []
    train_corrects = []
    train_precisions = []
    train_recalls = []
    train_f1scores = []

    model.train()
    logging.info('SET model mode to train!')

    t = trange(len(train_loader))
    logging.info(t)
    batch_iter = 0
    for batch_img1, batch_img2, labels in train_loader:
        logging.info("batch: "+str(batch_iter)+" - "+str(batch_iter+opt.batch_size))
        batch_iter = batch_iter+opt.batch_size
        batch_img1 = autograd.Variable(batch_img1).to(device)
        batch_img2 = autograd.Variable(batch_img2).to(device)

        labels = autograd.Variable(labels).long().to(device)

        optimizer.zero_grad()
        preds = model(batch_img1, batch_img2)
        loss = criterion(preds, labels)
        loss.backward()
        optimizer.step()

        _, preds = torch.max(preds, 1)

        corrects = 100 * (preds.byte() == labels.squeeze().byte()).sum() / (labels.size()[0] * opt.patch_size * opt.patch_size)

        train_report = prfs(labels.data.cpu().numpy().flatten(), preds.data.cpu().numpy().flatten(), average='binary', pos_label=1)

        train_losses.append(loss.item())
        train_corrects.append(corrects.item())
        train_precisions.append(train_report[0])
        train_recalls.append(train_report[1])
        train_f1scores.append(train_report[2])

        t.set_postfix(loss=loss.data.tolist(), accuracy=corrects.data.tolist())
        t.update()

        del batch_img1
        del batch_img2
        del labels

    train_loss = np.mean(train_losses)
    train_acc = np.mean(train_corrects)
    train_prec = np.mean(train_precisions)
    train_rec = np.mean(train_recalls)
    train_f1s = np.mean(train_f1scores)
    print('train loss : ', train_loss, ' train accuracy : ', train_acc, ' avg. precision : ', train_prec, 'avg. recall : ', train_rec, ' avg. f1 score : ', train_f1s)
    # fout.write('train loss : ' + str(train_loss) + ' train accuracy : ' + str(train_acc) + ' avg. precision : ' + str(train_prec) + ' avg. recall : ' + str(train_rec) + ' avg. f1 score : ' + str(train_f1s) + '\n')

    model.eval()

    test_losses = []
    test_corrects = []
    test_precisions = []
    test_recalls = []
    test_f1scores = []

    t = trange(len(test_loader))
    for batch_img1, batch_img2, labels in test_loader:
        batch_img1 = autograd.Variable(batch_img1).to(device)
        batch_img2 = autograd.Variable(batch_img2).to(device)

        labels = autograd.Variable(labels).long().to(device)
        labels = labels.view(-1, 1, opt.patch_size, opt.patch_size)

        preds = model(batch_img1, batch_img2)
        loss = criterion(preds, labels)

        _, preds = torch.max(preds, 1)

        corrects = 100 * (preds.byte() == labels.squeeze().byte()).sum() / (labels.size()[0] * opt.patch_size * opt.patch_size)

        test_report = prfs(labels.data.cpu().numpy().flatten(), preds.data.cpu().numpy().flatten(), average='binary', pos_label=1)

        test_losses.append(loss.item())
        test_corrects.append(corrects.item())
        test_precisions.append(test_report[0])
        test_recalls.append(test_report[1])
        test_f1scores.append(test_report[2])

        t.set_postfix(loss=loss.data.tolist(), accuracy=corrects.data.tolist())
        t.update()

        del batch_img1
        del batch_img2
        del labels

    test_loss = np.mean(test_losses)
    test_acc = np.mean(test_corrects)
    test_prec = np.mean(test_precisions)
    test_rec = np.mean(test_recalls)
    test_f1s = np.mean(test_f1scores)
    print ('test loss : ', test_loss, ' test accuracy : ', test_acc, ' avg. precision : ', test_prec, 'avg. recall : ', test_rec, ' avg. f1 score : ', test_f1s)

    if test_f1s > best_f1s:
        torch.save(model, '/tmp/checkpoint_epoch_'+str(epoch)+'.pt')
        experiment.outputs_store.upload_file('/tmp/checkpoint_epoch_'+str(epoch)+'.pt')
        best_f1s = test_f1s
        best_metric['train loss'] = str(train_loss)
        best_metric['test loss'] = str(test_loss)
        best_metric['train accuracy'] = str(train_acc)
        best_metric['test accuracy'] = str(test_acc)
        best_metric['train avg. precision'] = str(train_prec)
        best_metric['test avg. precision'] = str(test_prec)
        best_metric['train avg. recall'] = str(train_rec)
        best_metric['test avg. recall'] = str(test_rec)
        best_metric['train avg. f1 score'] = str(train_f1s)
        best_metric['test avg. f1 score'] = str(test_f1s)

    experiment.log_metrics(epoch=epoch,
                            train_f1_score=train_f1s,
                            train_recall=train_rec,
                            train_prec=train_prec,
                            train_loss=train_loss,
                            train_accuracy=train_acc,
                            test_f1_score=test_f1s,
                            test_recall=test_rec,
                            test_prec=test_prec,
                            test_loss=test_loss,
                            test_accuracy=test_acc)
