import wandb
import os
import torch
import torch.nn as nn
import torch.optim as optim

import albumentations as A
from albumentations.pytorch import ToTensorV2

from tqdm import tqdm
from datetime import datetime

from model import UNET
from early_stopping import EarlyStopping
from utils import (
    load_checkpoint,
    save_checkpoint,
    get_loaders,
    check_accuracy,
    save_predictions_as_imgs,
    save_validation_as_imgs,
    get_weights,
    print_and_save_results
)

# Initial Config

torch.manual_seed(19)
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
NUM_WORKERS = 12
PROJECT_NAME = "25_segmentation_4285_50_42"
PROJECT_TEAM = 'tail-upenn'
SCHEDULER = True
EARLYSTOP = True
PIN_MEMORY = True
LOAD_MODEL = False

# Hyperparameters

LEARNING_RATE = 1e-2 #[1. 1e-1, 1e-2, 1e-3, 1e-4, 1e-5]
BATCH_SIZE = 20
NUM_EPOCHS = 1000
OPTIMIZER = 'adam' #['adam']
MAX_LAYER_SIZE = 1024 #[512, 1024, 2056]
MIN_LAYER_SIZE = 64 #[64, 32, 16]
WEIGHTS = True

# Image Information

IMAGE_HEIGHT = 256
IMAGE_WIDTH = 98
IMAGE_CHANNELS = 1
MASK_CHANNELS = 1
MASK_LABELS = 4

# Paths
PARENT_DIR = os.path.abspath(__file__)
TRAIN_IMG_DIR = "data/train/phantom/"
TRAIN_MASK_DIR = "data/train/mask/"
VAL_IMG_DIR = "data/val/phantom/"
VAL_MASK_DIR = "data/val/mask/"
PREDICTIONS_DIR = "data/predictions/"
BEGIN = datetime.now().strftime("%Y%m%d_%H%M%S")

def train_fn(loader, model, optimizer, loss_fn, scaler, config):
    loop = tqdm(loader)
    # closs = 0

    for _, (data, targets) in enumerate(loop):
        data = data.to(device=DEVICE)
        targets = targets.long().to(device=DEVICE)
        
        # forward
        with torch.cuda.amp.autocast():
            predictions = model(data)
            loss = loss_fn(predictions, targets)

        # backward
        optimizer.zero_grad()
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()

        # update tqdm loop
        loop.set_postfix(loss=loss.item())

        # wandb logging
        wandb.log({"batch loss":loss.item()})
    #     closs += loss.item()
    
    # wandb.log({"loss":closs/config.batch_size})

    return loss.item()

def validate_fn(loader, model, loss_fn):
    loop = tqdm(loader)
    model.eval()
    
    for _, (data, targets) in enumerate(loop):
        data = data.to(DEVICE)
        targets = targets.long().to(DEVICE)

        with torch.no_grad():
            predictions = model(data)
            loss = loss_fn(predictions, targets)

        # update tqdm loop
        loop.set_postfix(loss=loss.item())

    model.train()
    
    return loss.item()

def train_loop(train_loader, val_loader, model, optimizer, scheduler, loss_fn, scaler, stopping, config):
    for epoch in range(NUM_EPOCHS):
        print('================================================================================================================================')
        print('BEGINNING EPOCH', epoch, ':')
        print('================================================================================================================================')        

        train_loss = train_fn(train_loader, model, optimizer, loss_fn, scaler, config)

        # save model
        checkpoint = {
            "state_dict": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "scheduler": scheduler.state_dict(),
        }

        save_checkpoint(checkpoint)

        # check accuracy
        accuracies = check_accuracy(val_loader, model, MASK_LABELS, DEVICE)
        val_loss = validate_fn(val_loader, model, loss_fn)
        metrics = accuracies[2]
        
        if SCHEDULER:
            scheduler.step(val_loss)

        dict_log = {"epoch":epoch,
                    "val_loss":val_loss,
                    "accuracy":metrics[0],
                    "label_0_accuracy":metrics[1][0],
                    "label_1_accuracy":metrics[1][1],
                    "label_2_accuracy":metrics[1][2],
                    "label_3_accuracy":metrics[1][3],
                    "label_0_recall":metrics[2][0],
                    "label_1_recall":metrics[2][1],
                    "label_2_recall":metrics[2][2],
                    "label_3_recall":metrics[2][3],
                   }
        
        
        print_and_save_results(accuracies[0], accuracies[1], metrics, train_loss, val_loss, BEGIN)
        
        # Print predictions to folder
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_predictions_as_imgs(val_loader, model, epoch, folder = PREDICTIONS_DIR, time=now, device = DEVICE)
        dict_log['prediction'] = wandb.Image(PREDICTIONS_DIR + f"{now}_pred_e{epoch}_i0.png")
        
        wandb.log(dict_log) 
        
        if EARLYSTOP:
            stopping(val_loss, checkpoint, checkpoint_path=f"checkpoints/{BEGIN}_best_checkpoint.pth.tar", epoch = epoch)
            
            if stopping.early_stop:
                print("Early Stopping ...")
                save_predictions_as_imgs(val_loader, model, epoch, folder = PREDICTIONS_DIR, device = DEVICE)
                #wandb.agent(sweep_id, train_fn)
                wandb.finish()
                break
    
    wandb.finish()
    print("Training Finished")

def main():

    config_defaults = {
        'epochs': NUM_EPOCHS,
        'batch_size': BATCH_SIZE,
        'learning_rate': LEARNING_RATE,
        'optimizer': OPTIMIZER,
        'max_layer_size': MAX_LAYER_SIZE,
        'min_layer_size': MIN_LAYER_SIZE
    }

    wandb.init(
        project = PROJECT_NAME,
        entity=PROJECT_TEAM,
        #group='experiment-1',
        config=config_defaults)

    config = wandb.config

    train_transforms, val_transforms = A.Compose([ToTensorV2(),],), A.Compose([ToTensorV2(),],)

    train_loader, val_loader = get_loaders(
        TRAIN_IMG_DIR,
        TRAIN_MASK_DIR,
        VAL_IMG_DIR,
        VAL_MASK_DIR,
        BATCH_SIZE,
        train_transforms,
        val_transforms,
        NUM_WORKERS,
        PIN_MEMORY,
    )

    model = UNET(in_channels = IMAGE_CHANNELS, classes = MASK_LABELS, config = config).to(DEVICE)

    if WEIGHTS:
        weights = get_weights(TRAIN_MASK_DIR, MASK_LABELS, DEVICE)
        loss_fn = nn.CrossEntropyLoss(weight = weights)
    else:
        loss_fn = nn.CrossEntropyLoss()
    
    if config.optimizer == 'adam':
        optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE)
    elif config.optimizer == 'sgd':
        optimizer = optim.SGD(model.parameters(), lr=LEARNING_RATE, momentum=0.9, nesterov=True, weight_decay=0.0001)
    else:
        raise KeyError(f"optimizer {config.optimizer} not recognized.")
    
    if SCHEDULER:
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min')

    if LOAD_MODEL:
        load_checkpoint(torch.load("my_checkpoint.pth.tar"), model, optimizer, scheduler)

    check_accuracy(val_loader, model, MASK_LABELS, DEVICE)
    save_validation_as_imgs(val_loader, folder = PREDICTIONS_DIR, device = DEVICE)

    scaler = torch.cuda.amp.GradScaler()

    stopping = EarlyStopping(patience = 15, wait = 20)

    train_loop(
        train_loader, 
        val_loader, 
        model, 
        optimizer, 
        scheduler, 
        loss_fn, 
        scaler, 
        stopping,
        config
    )

if __name__ == "__main__":
    main()