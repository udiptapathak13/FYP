import torch
import torch.backends.cudnn as cudnn
from tqdm import tqdm
import pandas as pd
import numpy as np
from utils.train_utils import AverageMeter, accuracy, module_copy, change_t

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class Learner():
    def __init__(self, train_config, ema=False, ir = False):
        '''
        train_conig: a dict of {'model_original': original full-precision transformer,
                                'model': model to use,
                                'loss_fn': loss function to use,
                                'optim_original': original optimizer setting,
                                'optim': optimizer to use,
                                'scheduler_original': original scheduler setting,
                                'scheduler': lr scheduler to use,
                                'datasets': dataset to use,
                                'epochs': how many epochs to train on,
                                'exp_name': experiment name, to distinguish different models,
                                'epoch_start_quantization': epoch to start quantization}
        ema: indicates whether it is a fully quant model
        ir:  indicates whether it is a ir network
        '''
        
        # training settings
        self.config = train_config

        self.model = train_config['model']
        self.model.to(device)
        self.criterion = train_config['loss_fn']
        self.optimizer = train_config['optim']
        self.scheduler = train_config['scheduler']
         
        if train_config.get("epoch_start_quantization"):
            self.starting_epoch = train_config['epoch_start_quantization']
        else:
            self.starting_epoch = 0
        
        # if original model is specified, that means we are doing latent quantization
        # else, we are doing normal training
        if train_config.get("model_original"):
            print("original model received!")
            self.original_model = train_config['model_original']
            self.original_model.to(device)
            self.original_optim = train_config["optim_original"]
            self.original_scheduler = train_config["scheduler_original"]
        else:
            assert self.starting_epoch == 0
            self.original_model = None

        self.train_loader, self.test_loader = train_config['datasets']

        # initialization of record variables
        self.test_acc_all = []
        self.best_acc = 0.0
        self.exp_name = train_config['exp_name']
        self.learning_log = {'Epoch': [],
                             'Iteration': [],
                             'Train Loss': [],
                             'Train Acc': []}

        # paths to save results
        self.log_path = f'./res/{self.exp_name}_log.csv'
        self.model_path = f'./res/{self.exp_name}_best.pth'
        self.test_path = f'./res/{self.exp_name}_test.npy'
        
        # indicator variables
        self.switch = False
        self.ema = ema
        self.ir = ir

    def train(self):
        cudnn.benchmark = True
        epochs = self.config['epochs']
        self.total_step = 120000 // self.config['batch_size']

        for epoch in tqdm(range(epochs)):
            print('current lr {:.5e}'.format(self.optimizer.param_groups[0]['lr']))
            
            # ir model needs to change t value every epoch
            if self.ir:
                t_value = 0.1*10**(epoch/epochs*np.log(100))
                t_value = torch.Tensor([t_value]).to(device)
                t_value.requires_grad = False
                print(t_value)
                
                change_t(self.model, t_value)
        
            if self.starting_epoch > epoch:
                self.original_model.train()
                self.train_step(epoch)
                self.original_model.eval()
                self.switch = True
            else:
                if self.original_model is not None and self.switch:
                    print("Switching to quantized model")
                    self.model = module_copy(self.original_model, self.model)
                    self.switch = False
                self.model.train()
                self.train_step(epoch)
                self.model.eval()

            test_acc = self.validate(epoch)
            self.test_acc_all.append(test_acc)

            if test_acc > self.best_acc:
                self.save_model()
                self.best_acc = test_acc

            if self.scheduler is not None:
                self.scheduler.step()

        # save learning log
        learn_df = pd.DataFrame.from_dict(self.learning_log)
        learn_df.to_csv(self.log_path)

        # save test set result
        np.save(self.test_path, self.test_acc_all)
        return self.test_acc_all

    def train_step(self, epoch, verbose=True):
        """
        Run one train epoch
        """
        losses = AverageMeter()
        top1_acc = AverageMeter()
        print(f'Epoch: {epoch}')
        print('\t\tLoss\t\tAccuracy')
        for i, (tokens, labels, masks) in enumerate(self.train_loader):

            # fetch batch data
            labels = labels.to(device) - 1
            tokens = tokens.to(device)
            masks = masks.to(device)

            # compute output
            if self.starting_epoch > epoch:
                logits = self.original_model(tokens, masks.view(masks.shape[0], 1, 1, masks.shape[1]))
                loss = self.criterion(logits, labels)
                # compute gradient and do SGD step
                self.original_optim.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), 10.)
                self.original_optim.step()
            else:
                logits = self.model(tokens, masks.view(masks.shape[0], 1, 1, masks.shape[1]))
                loss = self.criterion(logits, labels)
                # compute gradient and do SGD step
                self.optimizer.zero_grad()
                loss.backward()
#                 torch.nn.utils.clip_grad_norm_(self.model.parameters(), 10.)
                self.optimizer.step()
                if self.ema:
                    self.model.apply_ema()

            # measure accuracy and record loss
            prec1 = accuracy(logits.data, labels)[0]
            losses.update(loss.item(), labels.size(0))
            top1_acc.update(prec1.item(), labels.size(0))

            # measure elapsed time
            if i % 100 == 0:
                self.learning_log['Epoch'].append(epoch)
                self.learning_log['Iteration'].append(i)
                self.learning_log['Train Loss'].append(losses.avg)
                self.learning_log['Train Acc'].append(top1_acc.avg)
                if verbose:
                    print('{0:4}/{1}\t'
                          '{loss.avg:.2f}\t\t'
                          '{top1.avg:.2f}\t'.format(
                        i, self.total_step, loss=losses, top1=top1_acc))

    def validate(self, epoch, verbose=True):
        """
        Run evaluation
        """
        top1_acc = AverageMeter()

        with torch.no_grad():
            for tokens, labels, masks in self.test_loader:
                # fetch batch data
                labels = labels.to(device) - 1
                tokens = tokens.to(device)
                masks = masks.to(device)

                # compute output
                if self.starting_epoch > epoch and self.starting_epoch > 0:
                    logits = self.original_model(tokens, masks.view(masks.shape[0], 1, 1, masks.shape[1]))
                else:
                    logits = self.model(tokens, masks.view(masks.shape[0], 1, 1, masks.shape[1]))

                # measure accuracy and record loss
                prec1 = accuracy(logits.data, labels)[0]
                top1_acc.update(prec1.item(), labels.size(0))
        if verbose:
            print('Epoch[{}] *Validation*: Accuracy {top1.avg:.3f}'.format(epoch, top1=top1_acc))

        return top1_acc.avg

    def save_model(self):
        torch.save(self.model.state_dict(), self.model_path)

    def check_point(self):
        # not implemented yet
        pass
