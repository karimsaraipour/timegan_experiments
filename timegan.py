import os
import torch
import numpy as np
from networks import Embedder, Recovery, Generator, Discriminator, Supervisor
from utils import batch_generator, random_generator, MinMaxScaler, extract_time
from torch.utils.data.dataloader import DataLoader
from opacus import PrivacyEngine
from opacus.validators import ModuleValidator

torch.autograd.set_detect_anomaly(True)
class TimeGAN:
    def __init__(self, opt, ori_data):

        self.opt = opt
        self.ori_data, self.min_val, self.max_val = MinMaxScaler(ori_data)
        self.ori_time, self.max_seq_len = extract_time(self.ori_data)
        self.no, self.seq_len, self.z_dim = np.asarray(ori_data).shape
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

        # Create and initialize networks.
        self.para = dict()
        self.para['module'] = self.opt.module
        self.para['input_dim'] = self.z_dim
        self.para['hidden_dim'] = self.opt.hidden_dim
        self.para['num_layer'] = self.opt.num_layer
        self.embedder = Embedder(self.para).to(self.device)
        self.embedder = ModuleValidator.fix(self.embedder)
        self.recovery = Recovery(self.para).to(self.device)
        self.generator = Generator(self.para).to(self.device)
        self.discriminator = Discriminator(self.para).to(self.device)
        self.supervisor = Supervisor(self.para).to(self.device)

        # Create and initialize optimizer.
        self.optim_embedder = torch.optim.Adam(self.embedder.parameters(), lr=self.opt.lr)
        self.optim_recovery = torch.optim.Adam(self.recovery.parameters(), lr=self.opt.lr)
        self.optim_generator = torch.optim.Adam(self.generator.parameters(), lr=self.opt.lr)
        self.optim_discriminator = torch.optim.Adam(self.discriminator.parameters(), lr=self.opt.lr)
        self.optim_supervisor = torch.optim.Adam(self.supervisor.parameters(), lr=self.opt.lr)

        # Set loss function
        self.MSELoss = torch.nn.MSELoss()
        self.BCELoss = torch.nn.BCELoss()

        if self.opt.load_checkpoint:
            self.load_trained_networks()
        
        self.dataloader_static = DataLoader(self.ori_data, self.opt.batch_size)
        self.dataloader_time = DataLoader(self.ori_time, self.opt.batch_size)
        self.dataloader_static_iterator = iter(self.dataloader_static)
        self.dataloader_time_iterator = iter(self.dataloader_time)

    def gen_batch(self):

        # Set training batch
        try:
            self.X = next(self.dataloader_static_iterator).type(dtype=torch.float32).to(self.device)
            self.T = next(self.dataloader_time_iterator).type(dtype=torch.float32).to(self.device)
            self.Z = torch.rand(size=(self.opt.batch_size, self.max_seq_len, self.para['input_dim']), dtype=torch.float32).to(self.device)
        except StopIteration:
            pass

        # total networks forward
    def batch_forward(self):
        self.H = self.embedder(self.X)
        self.X_tilde = self.recovery(self.H)
        self.H_hat_supervise = self.supervisor(self.H)

        self.E_hat = self.generator(self.Z)
        self.H_hat = self.supervisor(self.E_hat)
        self.X_hat = self.recovery(self.H_hat)

        self.Y_real = self.discriminator(self.H)
        self.Y_fake = self.discriminator(self.H_hat)
        self.Y_fake_e = self.discriminator(self.E_hat)

    def gen_synth_data(self, batch_size):
        self.Z = random_generator(batch_size, self.para['input_dim'], self.max_seq_len, self.ori_time)
        self.Z = torch.tensor(self.Z, dtype=torch.float32).to(self.device)

        self.E_hat = self.generator(self.Z)
        self.H_hat = self.supervisor(self.E_hat)
        self.X_hat = self.recovery(self.H_hat)

        return self.X_hat
        
    def train_embedder(self, joint_train=False):
        # set models to training mode
        self.embedder.train()
        self.recovery.train()
        
        # privacy_engine = PrivacyEngine()

        # net, optimizer, trainloader = privacy_engine.make_private(
        #     module=self.embedder,
        #     optimizer=self.optim_embedder,
        #     data_loader=self.dataloader_static,
        #     noise_multiplier=1.1,
        #     max_grad_norm=1.1,
        # )
        
        # zero out any previous graidents
        self.optim_embedder.zero_grad()
        self.optim_recovery.zero_grad()
        
        # compute predictions
        self.H = self.embedder(self.X)
        self.X_tilde = self.recovery(self.H)
        
        # compute loss
        self.E_loss_T0 = self.MSELoss(self.X, self.X_tilde)
        self.E_loss0 = 10 * torch.sqrt(self.E_loss_T0)
        
        # backpropagation # E0_solver
        self.E_loss0.backward()

        # update weights
        self.optim_embedder.step()
        self.optim_recovery.step()

    def train_embedder(self, joint_train=False):
        # set models to training mode
        self.embedder.train()
        self.recovery.train()
        
        
        # DP bullshit
        privacy_engine = PrivacyEngine()

        net, optimizer, trainloader = privacy_engine.make_private(
            module=self.embedder,
            optimizer=self.optim_embedder,
            data_loader=self.dataloader_static,
            noise_multiplier=1.1,
            max_grad_norm=1.1,
        )
        
        try:
            self.X = next(iter(trainloader)).type(dtype=torch.float32).to(self.device)
        except StopIteration:
            pass
        
        # zero out any previous graidents
        optimizer.zero_grad()
        self.optim_recovery.zero_grad()
        
        # compute predictions
        self.H = net(self.X)
        self.X_tilde = self.recovery(self.H)
        
        # compute loss
        self.E_loss_T0 = self.MSELoss(self.X, self.X_tilde)
        self.E_loss0 = 10 * torch.sqrt(self.E_loss_T0)
        
        # backpropagation # E0_solver
        self.E_loss0.backward()

        # update weights
        self.optim_embedder.step()
        self.optim_recovery.step()
        
        
    def train_supervisor(self):
        # GS_solver
        self.generator.train()
        self.supervisor.train()
        self.optim_generator.zero_grad()
        self.optim_supervisor.zero_grad()
        self.G_loss_S = self.MSELoss(self.H[:, 1:, :], self.H_hat_supervise[:, :-1, :])
        self.G_loss_S.backward()
        self.optim_generator.step()
        self.optim_supervisor.step()

    def train_generator(self,joint_train=False):
        # G_solver
        self.optim_generator.zero_grad()
        self.optim_supervisor.zero_grad()
        self.G_loss_U = self.BCELoss(self.Y_fake, torch.ones_like(self.Y_fake))
        self.G_loss_U_e = self.BCELoss(self.Y_fake_e, torch.ones_like(self.Y_fake_e))
        self.G_loss_V1 = torch.mean(torch.abs(torch.sqrt(torch.std(self.X_hat, [0])[1] + 1e-6) - torch.sqrt(
            torch.std(self.X, [0])[1] + 1e-6)))
        self.G_loss_V2 = torch.mean(torch.abs((torch.mean(self.X_hat, [0])) - (torch.mean(self.X, [0]))))
        self.G_loss_V = self.G_loss_V1 + self.G_loss_V2
        self.G_loss_S = self.MSELoss(self.H_hat_supervise[:, :-1, :], self.H[:, 1:, :])
        self.G_loss = self.G_loss_U + \
                      self.opt.gamma * self.G_loss_U_e + \
                      torch.sqrt(self.G_loss_S) * 100 + \
                      self.G_loss_V * 100
        if not joint_train:
            self.G_loss.backward()
        else:
            self.G_loss.backward(retain_graph=True)

        self.optim_generator.step()
        self.optim_supervisor.step()


    def train_discriminator(self):
        # D_solver
        self.discriminator.train()
        self.optim_discriminator.zero_grad()
        self.D_loss_real = self.BCELoss(self.Y_real, torch.ones_like(self.Y_real))
        self.D_loss_fake = self.BCELoss(self.Y_fake, torch.zeros_like(self.Y_fake))
        self.D_loss_fake_e = self.BCELoss(self.Y_fake_e, torch.zeros_like(self.Y_fake_e))
        self.D_loss = self.D_loss_real + \
                      self.D_loss_fake + \
                      self.opt.gamma * self.D_loss_fake_e
        # Train discriminator (only when the discriminator does not work well)
        if self.D_loss > 0.15:
            self.D_loss.backward()
            self.optim_discriminator.step()

    def load_trained_networks(self):
        print("Loading trained networks")
        self.embedder.load_state_dict(torch.load(os.path.join(self.opt.networks_dir, 'embedder.pth')))
        self.recovery.load_state_dict(torch.load(os.path.join(self.opt.networks_dir, 'recovery.pth')))
        self.generator.load_state_dict(torch.load(os.path.join(self.opt.networks_dir, 'generator.pth')))
        self.discriminator.load_state_dict(torch.load(os.path.join(self.opt.networks_dir, 'discriminator.pth')))
        self.supervisor.load_state_dict(torch.load(os.path.join(self.opt.networks_dir, 'supervisor.pth')))
        print("Done.")

    def save_trained_networks(self):
        print("Saving trained networks")
        torch.save(self.embedder.state_dict(), os.path.join(self.opt.networks_dir, 'embedder.pth'))
        torch.save(self.recovery.state_dict(), os.path.join(self.opt.networks_dir, 'recovery.pth'))
        torch.save(self.generator.state_dict(), os.path.join(self.opt.networks_dir, 'generator.pth'))
        torch.save(self.discriminator.state_dict(), os.path.join(self.opt.networks_dir, 'discriminator.pth'))
        torch.save(self.supervisor.state_dict(), os.path.join(self.opt.networks_dir, 'supervisor.pth'))
        print("Done.")
