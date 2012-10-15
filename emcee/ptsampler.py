try:
    import acor
    acor = acor
except ImportError:
    acor=None
import emcee as em
import multiprocessing as multi
import numpy as np
import numpy.random as nr

__all__ = ["PTSampler"]

class PTPost(object):
    """Wrapper for posterior used in emcee."""
    
    def __init__(self, logl, logp, beta):
        """:param logl: Function returning natural log of the
            likelihood.

        :param logp: Function returning natural log of the prior.

        :param beta: Inverse temperature of this chain: lnpost =
            beta*logl + logp"""

        self._logl = logl
        self._logp = logp
        self._beta = beta

    def __call__(self, x):
        """
        :param x:
        The position in parameter space.

        Returns ``lnpost(x)``, ``lnlike(x)`` (the second value will be
        treated as a blob by emcee), where 

        .. math::

            \ln \pi(x) \equiv \beta \ln l(x) + \ln p(x)
        """

        lp = self._logp(x)

        # If outside prior bounds, return 0.
        if lp == float('-inf'):
            return lp, lp

        ll = self._logl(x)

        return self._beta*ll+lp, ll

class PTSampler(em.Sampler):
    """A parallel-tempered ensemble sampler, using ``EnsembleSampler``
    for sampling within each parallel chain.

    :param ntemps: 
        The number of temperatures.

    :param nwalkers: 
        The number of ensemble walkers at each temperature.

    :param dim: 
        The dimension of parameter space.

    :param logl: 
        The ln(likelihood) function.

    :param logp: 
        The ln(prior) function.

    :param threads: (optional)
        The number of parallel threads to use in sampling.

    :param pool: (optional) 
        Alternative to ``threads``.  Any object that implements a
        ``map`` method compatible with the built-in ``map`` will do
        here.  For example, :class:`multi.Pool` will do.

    :param betas: (optional) 
        Array giving the inverse temperatures, :math:`\\beta=1/T`, used in the
        ladder.  The default is for an exponential ladder, with beta
        decreasing by a factor of :math:`1/\\sqrt{2}` each rung."""
    def __init__(self, ntemps, nwalkers, dim, logl, logp, threads=1, pool=None, betas=None):
        self.logl=logl
        self.logp=logp

        self.ntemps = ntemps
        self.nwalkers = nwalkers
        self.dim = dim

        self._chain = None
        self._lnprob = None
        self._lnlikelihood = None

        if betas is None:
            self._betas = self.exponential_beta_ladder(ntemps)
        else:
            self._betas = betas

        self.nswap = np.zeros(ntemps, dtype=np.float)
        self.nswap_accepted = np.zeros(ntemps, dtype=np.float)

        self.pool = pool
        if threads > 1 and pool is None:
            self.pool = multi.Pool(threads)

        self.samplers = [em.EnsembleSampler(nwalkers, dim, PTPost(logl, logp, b), pool=self.pool) for b in self.betas]

    def exponential_beta_ladder(self, ntemps):
        """Exponential ladder in 1/T, with T increasing by sqrt(2)
        each step, with ``ntemps`` in total."""
        return np.exp(np.linspace(0, -(ntemps-1)*0.5*np.log(2), ntemps))

    def reset(self):
        """Clear the ``chain``, ``lnprobability``, ``lnlikelihood``,
        ``acceptance_fraction``, ``tswap_acceptance_fraction`` stored
        properties."""
    
        for s in self.samplers:
            s.reset()

        self.nswap = np.zeros(self.ntemps, dtype=np.float)
        self.nswap_accepted = np.zeros(self.ntemps, dtype=np.float)

        self._chain = None
        self._lnprob = None
        self._lnlikelihood = None

    def sample(self, p0, lnprob0=None, lnlike0=None, iterations=1, thin=1, storechain=True):
        """Advance the chains ``iterations`` steps as a generator.
        
        :param p0: 
            The initial positions of the walkers.  Shape should be
            ``(ntemps, nwalkers, dim)``.

        :param lnprob0: (optional) 
            The initial posterior values for the ensembles.  Shape
            ``(ntemps, nwalkers)``.

        :param lnlike0: (optional) 
            The initial likelihood values for the ensembles.  Shape
            ``(ntemps, nwalkers)``.

        :param iterations: (optional)
            The number of iterations to preform.

        :param thin: (optional)
            The number of iterations to perform between saving the
            state to the internal chain.

        :param storechain: (optional) 
            If ``True`` store the iterations in the ``chain``
            property.

        At each iteration, this generator yields

        * ``p``, the current position of the walkers.

        * ``lnprob`` the current posterior values for the walkers.

        * ``lnlike`` the current likelihood values for the walkers."""

        p = p0

        # If we have no lnprob or logls compute them
        if lnprob0 is None or lnlike0 is None:
            lnprob0=np.zeros((self.ntemps, self.nwalkers))
            lnlike0=np.zeros((self.ntemps, self.nwalkers))
            for i in range(self.ntemps):
                fn=PTPost(self.logl, self.logp, self.betas[i])
                if self.pool is None:
                    results=map(fn, p[i,:,:])
                else:
                    results=self.pool.map(fn, p[i,:,:])

                lnprob0[i,:] = np.array([r[0] for r in results])
                lnlike0[i,:] = np.array([r[1] for r in results])

        lnprob = lnprob0
        logl = lnlike0

        # Expand the chain in advance of the iterations
        if storechain:
            nsave=iterations/thin
            if self._chain is None:
                isave=0
                self._chain = np.zeros((self.ntemps, self.nwalkers, nsave, self.dim))
                self._lnprob = np.zeros((self.ntemps, self.nwalkers, nsave))
                self._lnlikelihood = np.zeros((self.ntemps, self.nwalkers, nsave))
            else:
                isave=self._chain.shape[2]
                self._chain = np.concatenate((self._chain, np.zeros((self.ntemps, self.nwalkers, nsave, self.dim))), axis=2)
                self._lnprob = np.concatenate((self._lnprob, np.zeros((self.ntemps, self.nwalkers, nsave))), axis=2)
                self._lnlikelihood = np.concatenate((self._lnlikelihood,
                                                     np.zeros((self.ntemps, self.nwalkers, nsave))), axis=2)

        for i in range(iterations):
            for j,s in enumerate(self.samplers):
                for psamp, lnprobsamp, rstatesamp, loglsamp in s.sample(p[j,...], lnprob0=lnprob[j,...], blobs0=logl[j,...], storechain=False):
                    p[j,...] = psamp
                    lnprob[j,...] = lnprobsamp
                    logl[j,...] = np.array(loglsamp)

            p,lnprob,logl = self._temperature_swaps(p, lnprob, logl)

            if (i+1)%thin == 0:
                if storechain:
                    self._chain[:,:,isave,:] = p
                    self._lnprob[:,:,isave,] = lnprob
                    self._lnlikelihood[:,:,isave] = logl
                    isave += 1

            yield p, lnprob, logl

    def _temperature_swaps(self, p, lnprob, logl):
        """Perform parallel-tempering temperature swaps on the state
        in ``p`` with associated ``lnprob`` and ``logl``."""

        ntemps=self.ntemps

        for i in range(ntemps-1, 0, -1):
            bi=self.betas[i]
            bi1=self.betas[i-1]

            dbeta = bi1-bi

            for j in range(self.nwalkers):
                self.nswap[i] += 1
                self.nswap[i-1] += 1

                ii=nr.randint(self.nwalkers)
                jj=nr.randint(self.nwalkers)

                paccept = dbeta*(logl[i, ii] - logl[i-1, jj])

                if paccept > 0 or np.log(nr.rand()) < paccept:
                    self.nswap_accepted[i] += 1
                    self.nswap_accepted[i-1] += 1

                    ptemp=np.copy(p[i, ii, :])
                    logltemp=logl[i, ii]
                    lnprobtemp=lnprob[i, ii]

                    p[i,ii,:]=p[i-1,jj,:]
                    logl[i,ii]=logl[i-1, jj]
                    lnprob[i,ii] = lnprob[i-1,jj] - dbeta*logl[i-1,jj]

                    p[i-1,jj,:]=ptemp
                    logl[i-1,jj]=logltemp
                    lnprob[i-1,jj]=lnprobtemp + dbeta*logltemp

        return p, lnprob, logl

    @property
    def betas(self):
        """Returns the sequence of inverse temperatures in the ladder."""
        return self._betas

    @property 
    def chain(self):
        """Returns the stored chain of samples; shape ``(Ntemps,
        Nwalkers, Nsteps, Ndim)``."""

        return self._chain

    @property
    def lnprobability(self):
        """Matrix of lnprobability values; shape ``(Ntemps, Nwalkers,
        Nsteps)``"""
        return self._lnprob

    @property
    def lnlikelihood(self):
        """Matrix of ln-likelihood values; shape ``(Ntemps, Nwalkers,
        Nsteps)``."""
        return self._lnlikelihood

    @property
    def tswap_acceptance_fraction(self):
        """Returns an array of accepted temperature swap fractions for
        each temperature; shape ``(ntemps, )``."""
        return self.nswap_accepted / self.nswap

    @property
    def acceptance_fraction(self):
        """Matrix of shape ``(Ntemps, Nwalkers)`` detailing the
        acceptance fraction for each walker."""
        return np.array([s.acceptance_fraction for s in self.samplers])

    @property
    def acor(self):
        """Returns a matrix of autocorrelation lengths for each
        parameter in each temperature of shape ``(Ntemps, Ndim)``."""
        
        if acor is None:
            raise ImportError('acor')
        else:
            acors=np.zeros((self.ntemps, self.dim))

            for i in range(self.ntemps):
                for j in range(self.dim):
                    acors[i,j] = acor.acor(self._chain[i, :, :, j])[0]

            return acors

    def thermodynamic_integration_log_evidence(self, fburnin=0.5):
        """Thermodynamic integration estimate of the evidence.

        :param fburnin: (optional) The fraction of the chain to
          discard as burnin samples; only the final ``1-fburnin``
          fraction of the samples will be used to compute the
          evidence.

        The evidence is the integral of the un-normalized posterior
        over all of parameter space:

        .. math:: 

            Z \\equiv \\int d\\theta \\, l(\\theta) p(\\theta)

        Thermodymanic integration is a technique for estimating the
        evidence integral using information from the chains at various
        temperatures."""

        betas=np.concatenate(self.betas, np.array([0]))
        logls=self.lnlikelihood

        istart=int(logls.shape[2]*fburnin) + 1

        mean_logls=np.mean(np.mean(logls, axis=1)[:, istart:], axis=1)
