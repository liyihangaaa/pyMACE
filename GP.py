import GPy
from GPyOpt.util.general import get_quantiles
import numpy as np
from math import pow, log, sqrt
import sys

# TODO: standardize the training data
class GP_MCMC:
    def __init__(self, train_x, train_y, B, num_init, warp = False, mcmc = True):
        
        self.mean = np.mean(train_y);
        self.std  = np.std(train_y);
        
        self.train_x   = train_x.copy()
        self.train_y   = (train_y - self.mean) / self.std
        self.num_train = self.train_x.shape[0]
        self.dim       = self.train_x.shape[1]
        self.B         = B
        self.num_init  = num_init
        self.warp      = warp
        self.mcmc      = mcmc

        kern = GPy.kern.Matern52(input_dim = self.dim, ARD = True)
        if self.warp:
            self.m = GPy.models.InputWarpedGP(self.train_x, self.train_y, kern)
        else:
            self.m = GPy.models.GPRegression(self.train_x, self.train_y, kern)

        # self.m.kern.variance.set_prior(GPy.priors.Gamma.from_EV(np.var(self.train_y), 120))
        # self.m.likelihood.variance.set_prior(GPy.priors.Gamma.from_EV(1e-2 * np.var(self.train_y), 4))
        # self.m.kern.lengthscale.set_prior(GPy.priors.Gamma.from_EV(np.std(self.train_x, 0), 1000 * np.ones(np.std(self.train_x, 0).shape)))
        
        self.m.kern.variance       = np.var(self.train_y)
        self.m.kern.lengthscale    = np.std(self.train_x, 0)
        self.m.likelihood.variance = np.maximum(2e-20, 1e-2 * np.var(self.train_y))
        self.m.likelihood.variance.constrain_bounded(1e-20, 1e10)

        self.m.kern.variance.set_prior(GPy.priors.LogGaussian(0, 1))
        self.m.likelihood.variance.set_prior(GPy.priors.Gamma.from_EV(0.02, 4))
        self.m.kern.lengthscale.set_prior(GPy.priors.LogGaussian(0, 10))

        self.eps     = 1e-3;
        self.upsilon = 0.5;
        self.delta   = 0.05
        self.tau     = np.min(train_y)

        self.burnin             = 200
        self.n_samples          = 10
        self.subsample_interval = 10
        self.sample()
        
    def sample(self):
        self.m.optimize(max_iters=200, messages=False)
        if not self.mcmc:
            self.s  = np.array(np.array(self.m[:]))
            self.s  = self.s.reshape(1, self.s.size)
            self.ms = np.array([self.m])
        else:
            hmc     = GPy.inference.mcmc.HMC(self.m,stepsize=5e-2)
            s       = hmc.sample(num_samples=self.burnin) # Burnin
            s       = hmc.sample(num_samples=self.n_samples * self.subsample_interval)
            self.s  = s[0::self.subsample_interval]
            self.ms = []
            for i in range(self.s.shape[0]):
                samp_kern = GPy.kern.Matern52(input_dim = self.dim, ARD = True)
                if self.warp:
                    samp_m = GPy.models.InputWarpedGP(self.train_x, self.train_y, samp_kern)
                else:
                    samp_m = GPy.models.GPRegression(self.train_x, self.train_y, samp_kern)
                samp_m[:] = self.s[i]
                samp_m.parameters_changed()
                self.ms = np.append(self.ms, samp_m)


    def predict_sample(self, x, hyp_vec):
        self.m.kern.variance       = hyp_vec[0]
        self.m.kern.lengthscale    = hyp_vec[1:1+self.dim]
        self.m.likelihood.variance = hyp_vec[1+self.dim]
        py, ps2                    = self.m.predict(x.reshape(1, x.size))
        py                         = self.mean + (py * self.std)
        ps2                        = ps2 * (self.std**2)
        return py, ps2;

    def set_kappa(self):
        num_train  = self.num_train
        t          = 1 + int((num_train - self.num_init) / self.B)
        self.kappa = sqrt(self.upsilon * 2 * log(pow(t, 2.0 + self.dim / 2.0) * 3 * pow(np.pi, 2) / (3 * self.delta)));

    def predict(self, x):
        num_samples = self.s.shape[0]
        pys         = np.zeros((num_samples, 1));
        pss         = np.zeros((num_samples, 1));
        for i in range(num_samples):
            m, v   = self.ms[i].predict(x.reshape(1, x.size))
            pys[i] = m[0][0]
            pss[i] = v[0][0]
        pys = self.mean + (pys * self.std)
        pss = pss * (self.std**2)
        return pys, np.sqrt(pss)

    def LCB(self, x, pys, pss):
        num_samples = pys.shape[0]
        self.set_kappa()
        acq = 0;
        for i in range(num_samples):
            y    = pys[i]
            s    = pss[i]
            lcb  = y - self.kappa * s
            acq += lcb
        acq /= self.s.shape[0]
        return acq

    def EI(self, x, pys, pss):
        num_samples = pys.shape[0]
        acq = 0;
        for i in range(num_samples):
            y            = pys[i]
            s            = pss[i]
            phi, Phi, u  = get_quantiles(self.eps, self.tau, y, s)
            f_acqu       = s * (u * Phi + phi)
            acq         += f_acqu
        acq /= self.s.shape[0]
        return acq

    def PI(self, x, pys, pss):
        num_samples = pys.shape[0]
        acq = 0;
        for i in range(num_samples):
            y          = pys[i]
            s          = pss[i]
            _, Phi, _  = get_quantiles(self.eps, self.tau, y, s)
            f_acqu     = Phi
            acq       += f_acqu
        acq /= self.s.shape[0]
        return acq

    def MACE_acq(self, x):
        pys, pss = self.predict(x);
        lcb      = self.LCB(x, pys, pss)
        ei       = self.EI(x, pys, pss)
        pi       = self.PI(x, pys, pss)
        return lcb, ei, pi
