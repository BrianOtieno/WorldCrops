import lightly
import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
from lightly.models.modules import NNMemoryBankModule
from lightly.models.modules.heads import ProjectionHead
from lightly.models.utils import batch_shuffle
from lightly.models.utils import batch_unshuffle
from lightly.models.utils import deactivate_requires_grad
from lightly.models.utils import update_momentum
import math
from lightly.models.modules.heads import SimSiamPredictionHead
from lightly.models.modules.heads import SimSiamProjectionHead

class SimSiam(nn.Module):
    def __init__(self, backbone = nn.Module, num_ftrs=64, proj_hidden_dim=14, pred_hidden_dim=14, out_dim=14):
        super().__init__()
        self.backbone = backbone
        self.model_type = 'SimSiam_nn.Module'
        self.projection = lightly.models.modules.heads.ProjectionHead([
            (num_ftrs, proj_hidden_dim, nn.BatchNorm1d(proj_hidden_dim), nn.ReLU()),
            (proj_hidden_dim, out_dim, nn.BatchNorm1d(out_dim), None)
        ])
        self.prediction = SimSiamPredictionHead(out_dim,pred_hidden_dim,out_dim)


    def forward(self, x0, x1):
        f0 = self.backbone(x0)
        f1 = self.backbone(x1)

        z0 = self.projection(f0)
        z1 = self.projection(f1)

        p0 = self.prediction(z0)
        p1 = self.prediction(z1)

        z0 = z0.detach()
        z1 = z1.detach()
        
        return (z0, p0),(z1, p1)





class SimSiam_LM(pl.LightningModule):
    def __init__(self, backbone = nn.Module, num_ftrs=64, proj_hidden_dim=14, 
    pred_hidden_dim=14, out_dim=14, lr=0.02, weight_decay=5e-4,momentum=0.9, epochs = 10):
        super().__init__()
        
        self.lr = lr
        self.momentum = momentum
        self.weight_decay = weight_decay
        self.epochs = epochs

        self.ce = lightly.loss.NegativeCosineSimilarity()
        self.backbone = backbone
        self.model_type = 'SimSiam_LM'
        self.projection = lightly.models.modules.heads.ProjectionHead([
            (num_ftrs, proj_hidden_dim, nn.BatchNorm1d(proj_hidden_dim), nn.ReLU()),
            (proj_hidden_dim, out_dim, nn.BatchNorm1d(out_dim), None)
        ])
        self.prediction = SimSiamPredictionHead(out_dim,pred_hidden_dim,out_dim)

        # parameters for logging
        self.avg_loss = 0.
        self.avg_output_std = 0.
        self.collapse_level = 0.
        self.out_dim = out_dim
        

    def forward(self, x0, x1):
        f0 = self.backbone(x0)
        f1 = self.backbone(x1)

        z0 = self.projection(f0)
        z1 = self.projection(f1)

        p0 = self.prediction(z0)
        p1 = self.prediction(z1)

        z0 = z0.detach()
        z1 = z1.detach()
        return (z0, p0),(z1, p1), f0


    def training_step(self, batch, batch_idx):
        (x0, x1), _, y = batch

        #print(x0.shape)
        #print(y.shape)

        (z0, p0),(z1, p1), embedding = self.forward(x0,x1)

        loss = 0.5 * (self.ce(z0, p1) + self.ce(z1, p0))

        #collapse based on https://docs.lightly.ai/tutorials/package/tutorial_simsiam_esa.html
        output = p0.detach()
        output = torch.nn.functional.normalize(output, dim=1)
        output_std = torch.std(output, 0)
        output_std = output_std.mean()

        # use moving averages to track the loss and standard deviation
        w = 0.9
        self.avg_loss = w * self.avg_loss + (1 - w) * loss.item()
        self.avg_output_std = w * self.avg_output_std + (1 - w) * output_std.item()

        self.logger.experiment.add_scalar('train_loss_ssl', loss, global_step=self.current_epoch)
        self.logger.experiment.add_scalar('Avgloss', self.avg_loss, global_step=self.current_epoch)
        self.logger.experiment.add_scalar('Avgstd', self.avg_output_std, global_step=self.current_epoch)
        return {'loss':loss, 'y_true': y.detach(), 'embedding': embedding.detach()}

    def training_epoch_end(self, outputs):
        # the level of collapse is large if the standard deviation of the l2
        # normalized output is much smaller than 1 / sqrt(dim)
        self.collapse_level = max(0., 1 - math.sqrt(self.out_dim) * self.avg_output_std)
        #self.log('Collapse Level', round(self.collapse_level,2), logger=True)
        self.logger.experiment.add_scalar('Collapse Level', round(self.collapse_level,2), global_step=self.current_epoch)

        embedding_list = list()
        y_true_list = list()

        for item in outputs:
            embedding_list.append(item['embedding'])
            y_true_list.append(item['y_true'])
        
        #log every 10 epochs
        if not self.current_epoch % 10:
            self.logger.experiment.add_embedding( torch.cat(embedding_list), metadata=torch.cat(y_true_list), global_step=self.current_epoch, tag = 'pretraining embedding') 


    def validation_step(self, val_batch, batch_idx):
        pass

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(self.parameters(), lr=self.lr,
                                momentum=self.momentum, weight_decay=self.weight_decay)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, self.epochs)
        return [optimizer], [scheduler]

#add this
#https://githubmemory.com/repo/PyTorchLightning/pytorch-lightning/issues/8302

