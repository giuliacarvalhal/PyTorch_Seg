import torch
import torch.functional as F
from torch.utils.data import DataLoader
from torchvision.utils import save_image
from cv2 import imread
from sklearn.metrics import precision_score, recall_score

import os
import csv
import numpy as np
from yaml import safe_load
from munch import munchify
import wandb
from datetime import datetime

from dataset import PhantomDataset

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
with open('config.yaml') as f:
    CONFIG = munchify(safe_load(f))

def save_checkpoint(state, filename="my_checkpoint.pth.tar"):
    torch.save(state, filename)

def load_checkpoint(checkpoint, model, optimizer, scheduler):
    print("=> Loading checkpoint")
    try:
        model.load_state_dict(checkpoint["state_dict"])
        if optimizer is not None:
            optimizer.load_state_dict(checkpoint["optimizer"])
        if scheduler is not None:
            scheduler.load_state_dict(checkpoint["scheduler"])
        return
    except KeyError:
        pass
    try:
        model.load_state_dict(checkpoint)
    except KeyError as e:
        raise ValueError(f'Key {e} is different from expected "state_dict"')
    return checkpoint["epoch"]

def get_loaders(train_dir, train_maskdir, val_dir, val_maskdir, test_dir, test_maskdir, batch_size, 
                train_transform, val_transform, test_transform, num_workers=4, pin_memory=True):

    train_ds = PhantomDataset(
        image_dir=train_dir,
        mask_dir=train_maskdir,
        transform=train_transform,
    )

    train_loader = DataLoader(
        train_ds,
        batch_size=batch_size,
        num_workers=num_workers,
        pin_memory=pin_memory,
        shuffle=True,
    )

    val_ds = PhantomDataset(
        image_dir=val_dir,
        mask_dir=val_maskdir,
        transform=val_transform,
    )

    val_loader = DataLoader(
        val_ds,
        batch_size=batch_size,
        num_workers=num_workers,
        pin_memory=pin_memory,
        shuffle=False,
    )

    if test_dir is not None:
        test_ds = PhantomDataset(
            image_dir=test_dir,
            mask_dir=test_maskdir,
            transform=test_transform,
        )

        test_loader = DataLoader(
            test_ds,
            batch_size=batch_size,
            num_workers=num_workers,
            pin_memory=pin_memory,
            shuffle=False,
        )
        
        return train_loader, val_loader, test_loader
    
    return train_loader, val_loader

def get_weights(mask_dir, num_labels, device=DEVICE, multiplier = CONFIG.HYPERPARAMETERS.MULTIPLIER):
    weights = np.zeros(num_labels)
    multiplier = np.array(multiplier)
    total_pixels = 0
    mask_files = [os.path.join(mask_dir, file) for file in os.listdir(mask_dir) if file.endswith('.png')]
    
    for mask in mask_files:
        mask = imread(mask)
    
        if total_pixels == 0:
            total_pixels = mask.shape[1] * mask.shape[2]

        temp = []
        
        for i in range(num_labels):
            temp.append((mask == i).sum())
        
        weights += temp
    den = weights / (total_pixels * len(mask))
    out = np.divide(multiplier, den, out = np.zeros_like(multiplier, dtype = float), where = den!=0)
    print(out)
    return torch.tensor(out).float().to(device)

def check_accuracy(loader, model, device=DEVICE):
    num_correct = 0
    num_pixels = 0
    
    model.eval()
    with torch.no_grad():
        for x, y in loader:
            x = x.to(device)
            preds = model(x)
            
            y = y.unsqueeze(1).to(device)

            preds_labels = torch.argmax(preds, 1).unsqueeze(1) #(14, 1, 512, 301) -> flatten (2157568,)
                
            num_correct += (preds_labels == y).sum()
            num_pixels += torch.numel(preds_labels)

            dict_eval = evaluate_segmentation(preds_labels, y, score_averaging = None)
    model.train()

    return num_correct, num_pixels, dict_eval

def log_predictions(val_loader, model, loss_train, loss_val, epoch, time=0, folder=CONFIG.PATHS.PREDICTIONS_DIR, device=DEVICE):
    num_correct, num_pixels, dict_eval  = check_accuracy(val_loader, model, device)

    dict_eval['loss_train'] = loss_train
    dict_eval['loss_val'] = loss_val

    print(f"Got {num_correct} of {num_pixels} pixels;")
    for key in dict_eval:
        print (key,':', dict_eval[key])

    with open(folder+f'{time}_preds.csv','a') as f:
        w = csv.DictWriter(f, dict_eval.keys())
        w.writeheader()
        w.writerow(dict_eval)
    
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_predictions_as_imgs(val_loader, model, epoch, dict_eval, time=now, folder=CONFIG.PATHS.PREDICTIONS_DIR, device=device)

def log_submission(loader, model, loss_test, time=0, folder=CONFIG.PATHS.PREDICTIONS_DIR, device = DEVICE):
    num_correct, num_pixels, dict_subm = check_accuracy(loader, model, device)
    dict_subm['loss_subm'] = loss_test

    print(f"Got {num_correct} of {num_pixels} pixels;")
    for key in dict_subm:
        print (key,':', dict_subm[key])

    with open(folder+f'{time}_submission.csv','a') as f:
        w = csv.DictWriter(f, dict_subm.keys())
        w.writeheader()
        w.writerow(dict_subm)
    
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_submission_as_imgs(loader, model, dict_subm, time=now, folder=CONFIG.PATHS.SUBMISSIONS_DIR, device=device)

def save_predictions_as_imgs(loader, model, epoch, dict_eval, folder=CONFIG.PATHS.PREDICTIONS_DIR, time=0, device=DEVICE):
    print("=> Saving predictions as images ...")
    
    model.eval()
    with torch.no_grad():    
        for idx, (x, _) in enumerate(loader):
            x = x.to(device)
            preds_labels = torch.argmax(model(x), 1)
            #print(preds_labels.unique(), "pred labels")
            preds_labels = label_to_pixel(preds_labels)
            #print(preds_labels.unique(), "pred labels to pixel")
            img = folder + f"{time}_pred_e{epoch}_i{idx}.png"
            save_image(preds_labels, img)
            dict_eval[f'prediction_i{idx}'] = wandb.Image(img)
            
            
    model.train()
    wandb.log(dict_eval)

def save_submission_as_imgs(loader, model, dict_subm, folder=CONFIG.PATHS.SUBMISSIONS_DIR, time=0, device=DEVICE):
    print("=> Saving submission images ...")
    
    model.eval()
    with torch.no_grad():    
        for idx, (x, _) in enumerate(loader):
            x = x.to(device)
            preds_labels = torch.argmax(model(x), 1)
            preds_labels = label_to_pixel(preds_labels)
            img = folder + f"{time}_submission_i{idx}.png"
            save_image(preds_labels, img)
            dict_subm[f'submission_i{idx}'] = wandb.Image(img)
            
    wandb.log(dict_subm)

def save_validation_as_imgs(loader, folder=CONFIG.PATHS.PREDICTIONS_DIR, time=0, device=DEVICE):
    print("=> Saving validation images ...")
    dict_val = {}
    for idx, (_, y) in enumerate(loader):
        y = y.to(device)
        #print(y.unique(), "y val")
        val = (y / y.max()).unsqueeze(1)
        #print(val.unique(), "val label")
        img = f"{folder}{time}_val_i{idx}.png"
        save_image(val, img)
        dict_val[f'validation_i{idx}'] = wandb.Image(img)
    
    wandb.log(dict_val)

def label_to_pixel(preds, col='l'):
    if col == 'l':
        preds = preds / (CONFIG.IMAGE.MASK_LABELS - 1) #0, 1, 2 
        preds = preds.unsqueeze(1).float()
        return preds

    else:
        preds = preds[:,1:,:,:]
        return preds.float()

def compute_iou(pred, label):
    pred, label = pred.cpu().numpy(), label.cpu().numpy()
    unique_labels = np.unique(label)
    num_unique_labels = len(unique_labels)

    I = np.zeros(num_unique_labels)
    U = np.zeros(num_unique_labels)

    for index, val in enumerate(unique_labels):
        
        pred_i = pred == val
        label_i = label == val
        I[index] = float(np.sum(np.logical_and(label_i, pred_i)))
        U[index] = float(np.sum(np.logical_or(label_i, pred_i)))

    return I / U

def dice_coef(y_pred,y_true):

    y_true_f = y_true.flatten()
    y_pred_f = y_pred.flatten()

    intersection = np.sum(y_true_f * y_pred_f)
    smooth = 0.0001
    return (2. * intersection + smooth) / (np.sum(y_true_f) + np.sum(y_pred_f) + smooth)

def dice_coef_multilabel(y_pred,y_true):
    num_labels = len(y_true.unique())
    dice=[]
    y_true, y_pred = y_true.cpu().numpy(), y_pred.cpu().numpy()
    
    for index in range(num_labels):
        dice.append(dice_coef(y_true == index, y_pred == index))

    return dice

# Compute the average segmentation accuracy across all classes
def compute_global_accuracy(pred, label):
    total = len(label)
    count = 0.0
    for i in range(total):
        if pred[i] == label[i]:
            count = count + 1.0
    return float(count) / float(total)

# Compute the class-specific segmentation accuracy
def compute_class_accuracies(pred, label, num_classes):
    total = []
    for val in range(num_classes):
        total.append((label == val).sum())

    count = [0.0] * num_classes
    for i in range(len(label)):
        if pred[i] == label[i]:
            count[int(pred[i])] = count[int(pred[i])] + 1.0

    # If there are no pixels from a certain class in the GT, 
    # it returns NAN because of divide by zero
    # Replace the nans with a 0.0.
    accuracies = []
    for i in range(len(total)):
        if total[i] == 0:
            accuracies.append(0.0)
        else:
            accuracies.append(count[i] / total[i])

    return accuracies

def evaluate_segmentation(pred, label, score_averaging=None):
    
    #iou = iou_score(pred, label, average=score_averaging)

    flat_pred = pred.flatten()
    flat_label = label.flatten()
    
    global_accuracy = compute_global_accuracy(flat_pred, flat_label)
    prec = precision_score(flat_pred, flat_label, average=score_averaging)
    rec = recall_score(flat_pred, flat_label, average=score_averaging, zero_division = 0)
    iou = compute_iou(flat_pred, flat_label)
    dice = dice_coef_multilabel(flat_pred, flat_label)
    
    dict_eval = {"accuracy":global_accuracy}

    for i in range(CONFIG.IMAGE.MASK_LABELS):
        dict_eval[f'accuracy_label_{i}'] = prec[i]
        dict_eval[f'recall_label_{i}'] = rec[i]
        dict_eval[f'iou_label_{i}'] = iou[i]
        #dict_eval[f'roc_score{i}'] = roc_score[i]
        dict_eval[f'dice_label_{i}'] = dice[i]

    return dict_eval