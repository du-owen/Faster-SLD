from datetime import datetime
import logging
import matplotlib.pyplot as plt
import numpy as np
import os
import pickle
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm
import time

from inference import *
from dataloader.indoor6 import *
from models.efficientlitesld import EfficientNetSLD
from models.custom_models import *
from utils.heatmap import generate_heat_maps_gpu


def plotting(ROOT_FOLDER):
    data = pickle.load(open('%s/stats.pkl' % ROOT_FOLDER, 'rb'))
    fig, axs = plt.subplots(4, 1)

    t = 0
    s = []
    epoch = 0
    for i in range(len(data['train'])-1):
        if data['train'][i+1]['ep'] == epoch + 1:
            epoch += 1
        else:
            t += 1
            s.append(data['train'][i]['loss'])

    t = np.arange(0, t)
    s = np.array(s)
    s = np.convolve(s, np.ones(10)/10., mode='same')

    axs[0].plot(t, np.log(s))
    axs[0].set(xlabel='iterations', ylabel='loss', title='')
    axs[0].grid()

    max_grad = np.array([data['train'][i]['max_grad'] for i in range(len(data['train']))])
    axs[1].plot(np.arange(0, len(max_grad)), np.log10(max_grad))
    axs[1].set(xlabel='iterations', ylabel='max gradient', title='')
    axs[1].grid()

    t = np.array([data['eval'][i]['ep'] for i in range(len(data['eval']))])
    s = np.array([np.median(data['eval'][i]['pixel_error']) for i in range(len(data['eval']))])
    axs[2].plot(t, s)
    axs[2].set(xlabel='epoch', ylabel='Pixel error', title='')
    axs[2].grid()
    axs[2].set_yticks(np.arange(0, 20, 5), minor=False)
    axs[2].set_ylim(0, 20)

    r = np.array([data['eval'][i]['recall'] for i in range(len(data['eval']))])
    axs[3].plot(t, r)
    axs[3].set(xlabel='epoch', ylabel='recall', title='')
    axs[3].grid()

    plt.subplots_adjust(left=None, bottom=None, right=None, top=None, wspace=0.8, hspace=1.0)
    plt.close()
    fig.savefig('%s/curve_train_test.png' % ROOT_FOLDER, format='png', dpi=120)

def train_patches(opt):

    if not os.path.exists(opt.output_folder):
        os.makedirs(opt.output_folder)

    logging.basicConfig(filename='%s/training.log' % opt.output_folder, filemode='a', level=logging.DEBUG, format='')
    logging.info("Scene Landmark Detector Training Patches")
    stats_pkl_logging = {'train': [], 'eval': []}

    device = opt.gpu_device

    assert len(opt.landmark_indices) == 0 or len(opt.landmark_indices) == 2, "landmark indices must be empty or length 2"
    train_dataset = Indoor6Patches(landmark_idx=np.arange(opt.landmark_indices[0],
                                                   opt.landmark_indices[1]) if len(opt.landmark_indices) == 2 else [None],
                            scene_id=opt.scene_id,
                            mode='train',
                            root_folder=opt.dataset_folder,
                            input_image_downsample=2,
                            landmark_config=opt.landmark_config,
                            visibility_config=opt.visibility_config,
                            skip_image_index=1)

    logging.info("indoor6 len {}".format(len(train_dataset)))

    train_dataloader = DataLoader(dataset=train_dataset, num_workers=4, batch_size=opt.training_batch_size, shuffle=True,
                                  pin_memory=True)
    
    ## Save the trained landmark configurations
    np.savetxt(os.path.join(opt.output_folder, 'landmarks.txt'), train_dataset.landmark)
    np.savetxt(os.path.join(opt.output_folder, 'visibility.txt'), train_dataset.visibility, fmt='%d')

    num_landmarks = train_dataset.landmark.shape[1]

    # if opt.model == 'bbv2_head_ace':
    path = opt.pretrained_path
    feats = 512
    model = MultiHeadModel(bb_model="bbresnetv2",head_version="v3",scenes=["placeholder"], path=path, features=feats, num_landmarks=num_landmarks)
    bb = model.bb.to(device=device)
    head = SceneHeadV4(features=feats, num_landmarks=num_landmarks).to(device=device)


    lowest_median_angular_error = 1e6

    feature_bank = FeatureDataset([],[],[])

    # creata dataloader of features
    for idx, batch in enumerate(tqdm(train_dataloader)):
        B1, B2, _, H, W = batch['patches'].shape
        B = B1 * B2
        patches = batch['patches']
        visibility = batch['visibility']
        landmark2d = batch['landmark2d']

        patches = patches.reshape(B, 3, H, W).to(device=device)
        visibility = visibility.reshape(B, num_landmarks).to(device=device)
        landmark2d = landmark2d.reshape(B, 2, num_landmarks).to(device=device)

        # Resolution configure
        landmark2d /= opt.output_downsample
        heat_map_size = [H // opt.output_downsample, W // opt.output_downsample]

        gt = generate_heat_maps_gpu(landmark2d,
                                    visibility,
                                    heat_map_size,
                                    sigma=torch.tensor([20. / opt.output_downsample], dtype=torch.float, device=device, requires_grad=False))
        gt.requires_grad = False
        # CNN forward pass
        with torch.no_grad():
            features = bb(patches)

        gt.to(device="cpu")
        features.to(device="cpu")

        # logging.info("len features: {}".format(features.shape))
        # logging.info("len hms: {}".format(gt.shape))
        # logging.info("len vis: {}".format(visibility.shape))


        # for i in range(features.shape[0]):
        feature_bank.features.append(features.to(device="cpu"))
        feature_bank.heatmaps.append(gt.to(device="cpu"))
        feature_bank.visibility.append(visibility.to(device="cpu"))

    # logging.info("len features: {}".format(len(feature_bank.features)))
    # logging.info("len hms: {}".format(len(feature_bank.heatmaps)))
    # logging.info("len vis: {}".format(len(feature_bank.visibility)))

    
    optimizer = torch.optim.AdamW(head.parameters(), lr=3e-3)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=40, gamma=0.5)
    
    feature_dataloader = DataLoader(dataset=feature_bank, num_workers=4, batch_size=1,
                                  pin_memory=True)

    for epoch in range(opt.num_epochs):
        # Training
        training_loss = 0
        for idx, batch in enumerate(tqdm(feature_dataloader)):

            # Clear gradient
            optimizer.zero_grad()

            # CNN forward pass
            # t = time.process_time()

            feature = batch["features"].squeeze().to(device=device)
            gt = batch["heatmap"].squeeze().to(device=device)
            visibility = batch["vis"].squeeze().to(device=device)

            # logging.info("feat {}".format(feature.shape))
            # logging.info("gt {}".format(gt.shape))
            # logging.info("vis {}".format(visibility.shape))

            pred = head(feature)
        
            # logging.info("forward pass:{}".format(time.process_time()-t))

            # t = time.process_time()
            # Compute loss and do backward pass
            losses = torch.sum((pred[visibility != 0.5] - gt[visibility != 0.5]) ** 2)

            training_loss += losses.detach().clone().item()
            losses.backward()

            # m = torch.tensor([0.0]).to(device)
            # for p in head.parameters():
            #     m = torch.max(torch.max(torch.abs(p.grad.data)), m)

            ## Ignore batch with large gradient element
            # if epoch == 0 or (epoch > 0 and m < 1e4):
            optimizer.step()
            
            # else:
            #     model.load_state_dict(torch.load('%s/model-best_median.ckpt' % (opt.output_folder)))
            #     bb = model[0].to(device=device)
            #     for param in bb.parameters():
            #         param.requires_grad = False
            #     head = model[1].to(device=device)

            logging.info('epoch %d, iter %d, loss %4.4f' % (epoch, idx, losses.item()))
            stats_pkl_logging['train'].append({'ep': epoch, 'iter': idx, 'loss': losses.item(), 'max_grad': np.array([0])}) # m.cpu().numpy()
            # logging.info("loss + backward pass:{}".format(time.process_time()-t))

        # Saving the ckpt
        scheduler.step()

        if epoch % 10 == 0:
            path = '%s/head-latest.ckpt' % (opt.output_folder)
            torch.save(head.state_dict(), path)
            opt.pretrained_model = [path]
            eval_stats = inference(opt, opt_tight_thr=1e-3, minimal_tight_thr=1e-3, mode='val')

            median_angular_error = np.median(eval_stats['angular_error'])
            path = '%s/head-best_median.ckpt' % (opt.output_folder)

            if (median_angular_error < lowest_median_angular_error):
                lowest_median_angular_error = median_angular_error
                torch.save(head.state_dict(), path)
            
            if (~os.path.exists(path) and len(eval_stats['angular_error']) == 0):
                torch.save(head.state_dict(), path)

            # date time
            ts = datetime.now().timestamp()
            dt = datetime.fromtimestamp(ts)
            datestring = dt.strftime("%Y-%m-%d_%H-%M-%S")

            # Print, log and update plot
            stats_pkl_logging['eval'].append(
                {'ep': epoch,
                'angular_error': eval_stats['angular_error'],
                'pixel_error': eval_stats['pixel_error'],
                'recall': eval_stats['r5p5']
                })


            try:
                str_log = 'epoch %3d: [%s] ' \
                        'tr_loss= %10.2f, ' \
                        'lowest_median= %8.4f deg. ' \
                        'recall= %2.4f ' \
                        'angular-err(deg.)= [%7.4f %7.4f %7.4f]  ' \
                        'pixel-err= [%4.3f %4.3f %4.3f] [mean/med./min] ' % (epoch, datestring, training_loss,
                                                                                lowest_median_angular_error,
                                                                                eval_stats['r5p5'],
                                                                                np.mean(eval_stats['angular_error']),
                                                                                np.median(eval_stats['angular_error']),
                                                                                np.min(eval_stats['angular_error']),
                                                                                np.mean(eval_stats['pixel_error']),
                                                                                np.median(eval_stats['pixel_error']),
                                                                                np.min(eval_stats['pixel_error']))
                print(str_log)
                logging.info(str_log)
            except ValueError:  #raised if array is empty.
                str_log = 'epoch %3d: [%s] ' \
                            'tr_loss= %10.2f, ' \
                            'No correspondences found' % (epoch, datestring, training_loss)
                print(str_log)
                logging.info(str_log)

            with open('%s/stats.pkl' % opt.output_folder, 'wb') as f:
                pickle.dump(stats_pkl_logging, f)
            plotting(opt.output_folder)
