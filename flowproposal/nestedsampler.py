from collections import deque
import datetime
import logging
import os
import pickle
import time

import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import ksone
import seaborn as sns
import torch
from tqdm import tqdm

from .livepoint import live_points_to_array, get_dtype
from .plot import plot_indices
from .posterior import logsubexp, log_integrate_log_trap
from .utils import safe_file_dump

sns.set()
sns.set_style('ticks')

logger = logging.getLogger(__name__)


class _NSintegralState(object):
  """
  Stores the state of the nested sampling integrator
  """
  def __init__(self, nlive):
    self.nlive = nlive
    self.reset()
  def reset(self):
    """
    Reset the sampler to its initial state at logZ = -infinity
    """
    self.iteration=0
    self.logZ=-np.inf
    self.oldZ=-np.inf
    self.logw=0
    self.info=0
    # Start with a dummy sample enclosing the whole prior
    self.logLs=[-np.inf] # Likelihoods sampled
    self.log_vols=[0.0] # Volumes enclosed by contours
    self.gradients = [0]
  def increment(self, logL, nlive=None):
    """
    Increment the state of the evidence integrator
    Simply uses rectangle rule for initial estimate
    """
    if(logL<=self.logLs[-1]):
      logger.warning('NS integrator received non-monotonic logL. {0:.5f} -> {1:.5f}'.format(self.logLs[-1],logL))
    if nlive is None:
      nlive = self.nlive
    oldZ = self.logZ
    logt=-1.0/nlive
    Wt = self.logw + logL + logsubexp(0,logt)
    self.logZ = np.logaddexp(self.logZ,Wt)
    # Update information estimate
    if np.isfinite(oldZ) and np.isfinite(self.logZ) and np.isfinite(logL):
        self.info = np.exp(Wt - self.logZ)*logL + np.exp(oldZ - self.logZ)*(self.info + oldZ) - self.logZ
        if np.isnan(self.info):
            self.info=0

    # Update history
    self.logw += logt
    self.iteration += 1
    self.logLs.append(logL)
    self.log_vols.append(self.logw)
    self.gradients.append((self.logLs[-1] - self.logLs[-2]) / (self.log_vols[-1] - self.log_vols[-2]))
  def finalise(self):
    """
    Compute the final evidence with more accurate integrator
    Call at end of sampling run to refine estimate
    """
    from scipy import integrate
    # Trapezoidal rule
    self.logZ = log_integrate_log_trap(np.array(self.logLs),np.array(self.log_vols))
    return self.logZ
  def plot(self,filename):
    """
    Plot the logX vs logL
    """
    import matplotlib as mpl
    mpl.use('Agg')
    from matplotlib import pyplot as plt
    fig=plt.figure()
    plt.plot(self.log_vols,self.logLs)
    plt.title('{0} iterations. logZ={1:.2f} H={2:.2f} bits'.format(self.iteration,self.logZ,self.info*np.log2(np.e)))
    plt.grid(which='both')
    plt.xlabel('log prior_volume')
    plt.ylabel('log likelihood')
    plt.xlim([self.log_vols[-1],self.log_vols[0]])
    plt.savefig(filename)
    logger.info('Saved nested sampling plot as {0}'.format(filename))


class NestedSampler:
    """
    Nested Sampler class.
    Initialisation arguments:

    model: :obj:`cpnest.Model` user defined model

    manager: `multiprocessing` manager instance which controls
        the shared objects.
        Default: None

    nlive: int
        number of live points to be used for the integration
        Default: 1024

    output: string
        folder where the output will be stored
        Default: None

    verbose: int
        0: Nothing
        1: display information on screen
        2: (1) + diagnostic plots
        Default: 1

    seed: int
        seed for the initialisation of the pseudorandom chain
        Default: 1234

    prior_sampling: boolean
        produce nlive samples from the prior.
        Default: False

    stopping: float
        Stop when remaining samples wouldn't change logZ estimate by this much.
        Deafult: 0.1

    n_periodic_checkpoint: int
        checkpoint the sampler every n_periodic_checkpoint iterations
        Default: None (disabled)

    """

    def __init__(self, model, nlive=1000, output=None, prior_sampling=False,
                 stopping=0.1, flow_class=None, flow_config={}, train_on_empty=True,
                 cooldown=100, memory=False, acceptance_threshold=0.05, analytic_priors = False,
                 maximum_uninformed=1000, training_frequency=1000, uninformed_proposal=None,
                 reset_weights=True, checkpointing=True, resume_file=None,
                 uninformed_proposal_kwargs={}, seed=None, plot=True, force_train=True,
                 proposal_plots=True, max_iteration=None,
                 **kwargs):
        """
        Initialise all necessary arguments and
        variables for the algorithm
        """
        logger.info('Initialising nested sampler')
        self.model          = model
        self.prior_sampling = prior_sampling
        self.setup_random_seed(seed)
        self.verbose        = 3
        self.accepted       = 0
        self.rejected       = 1
        self.last_updated = 0
        self.iteration      = 0
        self.acceptance_history = deque(maxlen=(nlive // 10))
        self.mean_acceptance_history = []
        self.block_acceptance = 1.
        self.mean_block_acceptance = 1.
        self.block_iteration = 0
        self.nlive          = nlive
        self.live_points = None
        self.insertion_indices = []
        self.rolling_p      = []
        self.checkpointing = checkpointing
        self.resumed = False
        self.tolerance      = stopping
        self.condition      = np.inf
        self.worst          = 0
        self.logLmin        = -np.inf
        self.logLmax        = -np.inf
        self.nested_samples = []
        self.logZ           = None
        self.state          = _NSintegralState(self.nlive)
        self.plot = plot
        self.output_file, self.evidence_file, self.resume_file = \
                self.setup_output(output, resume_file)
        self.output = output
        header              = open(os.path.join(output,'header.txt'),'w')
        header.write('\t'.join(self.model.names))
        header.write('\tlogL\n')

        header.close()

        self.training_time = 0
        self.likelihood_evaluations = []
        self.training_iterations = []
        self.likelihood_calls = 0
        self.min_likelihood = []
        self.max_likelihood = []
        self.logZ_history = []
        self.dZ_history = []
        self.population_acceptance = []
        self.population_iterations = []


        if max_iteration is None:
            self.max_iteration = np.inf
        else:
            self.max_iteration = max_iteration

        self.acceptance_threshold = acceptance_threshold
        self.train_on_empty = train_on_empty
        self.force_train = True
        self.cooldown = cooldown
        self.memory = memory
        self.reset_weights = float(reset_weights)
        if training_frequency in [None, 'inf', 'None']:
            logger.warning('Proposal will only train when empty')
            self.training_frequency = np.inf
        else:
            self.training_frequency = training_frequency

        self.max_count = 0

        self.initialised    = False

        logger.info(f'Parsing kwargs to FlowProposal: {kwargs}')
        proposal_output = self.output + '/proposal/'
        if flow_class is not None:
            if isinstance(flow_class, str):
                if flow_class == 'GWFlowProposal':
                    from .gw.proposal import GWFlowProposal
                    flow_class = GWFlowProposal
                elif flow_class == 'FlowProposal':
                    from .proposal import FlowProposal
                    flow_class = FlowProposal
                else:
                    raise RuntimeError(f'Unknown flow class: {flow_class}')
            self._flow_proposal = flow_class(model, flow_config=flow_config,
                    output=proposal_output, plot=proposal_plots, **kwargs)
        else:
            from .proposal import FlowProposal
            self._flow_proposal = FlowProposal(model, flow_config=flow_config,
                    output=proposal_output, plot=proposal_plots, **kwargs)


        # Uninformed proposal is used for prior sampling
        # If maximum uninformed is greater than 0, the it will be used for
        # another n interation or until it becomes inefficient
        if uninformed_proposal is not None:
            self._uninformed_proposal = unfinformed_proposa(model,
                    **uninformed_proposal_kwargs)
        else:
            if analytic_priors:
                from .proposal import AnalyticProposal
                self._uninformed_proposal = AnalyticProposal(model,
                        **uninformed_proposal_kwargs)
            else:
                from .proposal import RejectionProposal
                self._uninformed_proposal = RejectionProposal(model, poolsize=self.nlive,
                        **uninformed_proposal_kwargs)

        if not maximum_uninformed or maximum_uninformed is None:
            self.uninformed_sampling = False
            self.maximum_uninformed = 0
        else:
            self.uninformed_sampling = True
            self.maximum_uninformed = maximum_uninformed


        self.store_live_points = False
        if self.store_live_points:
            self.live_points_dir = f'{self.output}/live_points/'
            os.makedirs(self.live_points_dir, exist_ok=True)
            self.replacement_points = []

    def setup_output(self, output, resume_file=None):
        """
        Set up the output folder

        -----------
        Parameters:
        output: string
            folder where the results will be stored
        -----------
        Returns:
            output_file, evidence_file, resume_file: tuple
                output_file:   file where the nested samples will be written
                evidence_file: file where the evidence will be written
                resume_file:   file used for checkpointing the algorithm
        """
        if not os.path.exists(output):
            os.makedirs(output, exist_ok=True)
        chain_filename = "chain_" + str(self.nlive) + ".txt"
        output_file   = os.path.join(output,chain_filename)
        evidence_file = os.path.join(output,chain_filename+"_evidence.txt")
        if resume_file is None:
            resume_file  = os.path.join(output,"nested_sampler_resume.pkl")
        else:
            resume_file  = os.path.join(output, resume_file)

        if self.plot:
            os.makedirs(output + '/diagnostics/', exist_ok=True)

        return output_file, evidence_file, resume_file


    def write_nested_samples_to_file(self):
        """
        Writes the nested samples to a text file
        """
        ns = np.array(self.nested_samples)
        np.savetxt(self.output_file, ns,
                header='\t'.join(self.live_points.dtype.names))

    def write_evidence_to_file(self):
        """
        Write the evidence logZ and maximum likelihood to the evidence_file
        """
        with open(self.evidence_file,"w") as f:
            f.write('{0:.5f} {1:.5f} {2:.5f}\n'.format(self.state.logZ, self.logLmax, self.state.info))

    def setup_random_seed(self, seed):
        """
        initialise the random seed
        """
        self.seed = seed
        if self.seed is not None:
            np.random.seed(seed=self.seed)
            torch.manual_seed(self.seed)

    def check_insertion_indices(self, rolling=True, filename=None):
        """
        Checking the distibution of the insertion indices either during
        the nested sampling run (rolling=True) or for the whole run
        (rolling=False).
        """
        if rolling:
            indices = self.insertion_indices[-self.nlive:]
        else:
            indices = self.insertion_indices

        analytic_cdf = np.arange(self.nlive + 1) / self.nlive
        counts, _ = np.histogram(indices, bins=np.arange(self.nlive + 1))
        cdf = np.cumsum(counts) / len(indices)
        gaps = np.column_stack([cdf - analytic_cdf[:self.nlive],
            analytic_cdf[1:] - cdf])
        D = np.max(gaps)
        p = ksone.sf(D, self.nlive)

        if rolling:
            logger.warning('Rolling KS test: D={0:.3}, p-value={1:.3}'.format(D, p))
            self.rolling_p.append(p)
        else:
            logger.warning('Final KS test: D={0:.3}, p-value={1:.3}'.format(D, p))

        if filename is not None:
            np.savetxt(os.path.join(
                self.output, filename),
                self.insertion_indices,
                newline='\n',delimiter=' ')

    def log_likelihood(self, x):
        """
        Wrapper for the model likelihood so evaluations are counted
        """
        return self.model.log_likelihood(x)

    def yield_sample(self, oldparam):
        """
        Draw points and applying rejection sampling
        """
        while True:
            counter = 0
            while True:
                counter += 1
                newparam = self.proposal.draw(oldparam.copy())
                newparam['logP'] = self.model.log_prior(newparam)

                if newparam['logP'] != -np.inf:
                    if not newparam['logL']:
                        newparam['logL'] = self.model.evaluate_log_likelihood(newparam)
                    if newparam['logL'] > self.logLmin:
                        self.logLmax= max(self.logLmax, newparam['logL'])
                        oldparam = newparam.copy()
                        break
                if (1 / counter) < self.acceptance_threshold:
                    self.max_count += 1
                    break
                # Only here if proposed and then empty
                # This returns the old point and allows for a training check
                if not self.proposal.populated:
                    break
            yield counter, oldparam

    def insert_live_point(self, live_point):
        """
        Insert a live point
        """
        # This is the index including the current worst point, so final index
        # is one less, otherwise index=0 would never be possible
        index = np.searchsorted(self.live_points['logL'], live_point['logL'])
        # Concatentate is complied C code, so it is much faster than np.insert
        # it also allows for simultaneous removal of the worst point
        # and insertion of the new live point
        self.live_points = np.concatenate([self.live_points[1:index], [live_point],
            self.live_points[index:]])
        return index - 1

    def consume_sample(self):
        """
        Replace a sample for single thread
        """
        worst = self.live_points[0]    # Should this be a copy?
        self.logLmin = worst['logL']
        self.state.increment(worst['logL'])
        self.nested_samples.append(worst)

        self.condition = np.logaddexp(self.state.logZ,
                self.logLmax - self.iteration/(float(self.nlive))) - self.state.logZ

        # Replace the points we just consumed with the next acceptable ones
        # Make sure we are mixing the chains
        self.iteration += 1
        self.block_iteration += 1
        count = 0

        while(True):
            c, proposed = next(self.yield_sample(worst))
            count += c

            if proposed['logL'] > self.logLmin:
                # Assuming point was proposed
                # replace worst point with new one
                index = self.insert_live_point(proposed)
                self.insertion_indices.append(index)
                self.accepted += 1
                self.block_acceptance += 1 / count
                self.acceptance_history.append(1 / count)
                break
            else:
                self.rejected += 1
                self.check_state(rejected=True)
                # if retrained in whilst proposing a sample then update the
                # iteration count since will be zero otherwise
                if not self.block_iteration:
                    self.block_iteration += 1

        if self.store_live_points:
            self.replacement_points.append(proposed)

        self.acceptance = self.accepted / (self.accepted + self.rejected)
        self.mean_block_acceptance = self.block_acceptance / self.block_iteration
        logger.info((f"{self.iteration:5d}: n: {count:3d} "
            f"NS_acc: {self.acceptance:.3f} m_acc: {self.mean_acceptance:.3f} "
            f"b_acc: {self.mean_block_acceptance:.3f} sub_acc: {1 / count:.3f} "
            f"H: {self.state.info:.2f} logL: {self.logLmin:.5f} --> "
            f"{proposed['logL']:.5f} dZ: {self.condition:.3f} "
            f"logZ: {self.state.logZ:.3f} logLmax: {self.logLmax:.2f}"))

    def populate_live_points(self):
        """
        Initialise the pool of `cpnest.parameter.LivePoint` by
        sampling them from the `cpnest.model.log_prior` distribution
        """
        # send all live points to the samplers for start
        i = 0
        live_points = np.array([], dtype=get_dtype(self.model.names, 'f8'))
        with tqdm(total=self.nlive, disable= not self.verbose, desc='Drawing live points') as pbar:
            while i < self.nlive:
                while i < self.nlive:
                    count, live_point = next(self.yield_sample(self.model.new_point()))
                    if np.isnan(live_point['logL']):
                        logger.warning("Likelihood function returned NaN for live_points " + str(live_points[i]))
                        logger.warning("You may want to check your likelihood function")
                    if live_point['logP']!=-np.inf and live_point['logL'] != -np.inf:
                        i+=1
                        live_points = np.concatenate([live_points, [live_point]])
                        pbar.update()
                        break

        self.live_points= np.sort(live_points, order='logL')
        if self.store_live_points:
            np.savetxt(self.live_points_dir + '/intial_live_points.dat', self.live_points,
                    header='\t'.join(self.live_points.dtype.names))

    def initialise(self, live_points=True):
        """
        Initialise the nested sampler
        """
        flags = [False] * 3
        if not self._flow_proposal.initialised:
            self._flow_proposal.initialise()
            flags[0] = True

        if not self._uninformed_proposal.initialised:
            self._uninformed_proposal.initialise()
            flags[1] = True

        if self.iteration < self.maximum_uninformed:
            self.proposal = self._uninformed_proposal
        else:
            self.proposal = self._flow_proposal

        if live_points and self.live_points is None:
            self.populate_live_points()
            flags[2] = True

        if all(flags):
            self.initialised = True

    @property
    def mean_acceptance(self):
        """
        Mean acceptance of the last nlive // 10 points
        """
        return np.mean(self.acceptance_history)

    def check_state(self, force=False, rejected=False):
        """
        Check if state should be updated prior to drawing a new sample

        Force will overide the cooldown mechanism, rejected will not
        """
        if self.uninformed_sampling:
            if (self.mean_acceptance < 10 * self.acceptance_threshold) or \
                    (self.iteration >= self.maximum_uninformed):
                logger.warning('Switching to FlowProposal')
                self.proposal = self._flow_proposal
                self.uninformed_sampling = False
            # If using uninformed sampling, don't check training
            else:
                return
        # Should the proposal be trained
        train = False
        # General overide
        if force:
            train = True
            logger.debug('Training flow (force)')
        elif self.mean_block_acceptance < self.acceptance_threshold and \
                self.iteration - self.last_updated < self.cooldown:
            train = True
            logger.debug('Training flow (acceptance)')
        elif rejected and self.mean_block_acceptance < self.acceptance_threshold:
            logger.debug('Training flow (rejected + acceptance)')
            train = True

        elif not (self.iteration - self.last_updated) % self.training_frequency:
            train = True
            logger.debug('Training flow (iteration)')

        # Check for empty should be independent of other checks
        if not self.proposal.populated:
            if self.train_on_empty:
                train = True
                if self.force_train:
                    force = True
                logger.debug('Training flow (proposal empty)')

        if train:
            if self.iteration - self.last_updated < self.cooldown and not force:
                logger.debug('Not training, still cooling down!')
            elif self.resumed:
                logger.info('Skipping training because of resume')
                self.resumed = False
            else:
                if self.reset_weights and not (self.proposal.training_count % self.reset_weights):
                    self.proposal.reset_model_weights()
                training_data = self.live_points.copy()
                if self.memory:
                    if len(self.nested_samples):
                        if len(self.nested_samples) >= self.memory:
                            training_data = np.concatenate([training_data, self.nested_samples[-self.memory].copy()])
                st = time.time()
                self.proposal.train(training_data)
                self.training_time += (time.time() - st)
                self.training_iterations.append(self.iteration)
                self.last_updated = self.iteration
                self.block_iteration = 0
                self.block_acceptance = 0.
                if self.checkpointing:
                    self.checkpoint()

        #if not self.proposal.populated:
            #self.proposal.populate(self.live_points[:10], N=self.proposal.poolsize)

    def plot_state(self):
        """
        Produce plots with the current state of the nested sampling run
        """

        fig, ax = plt.subplots(6, 1, sharex=True, figsize=(12,12))
        ax = ax.ravel()
        it = (np.arange(len(self.min_likelihood))) * (self.nlive // 10)
        it[-1] = self.iteration
        ax[0].plot(it, self.min_likelihood, label='Min logL', c='lightblue')
        ax[0].plot(it, self.max_likelihood, label='Max logL', c='darkblue')
        ax[0].set_ylabel('logL')
        ax[0].legend(frameon=False)

        g = np.min([len(self.state.gradients), self.iteration])
        ax[1].plot(np.arange(g), np.abs(self.state.gradients[:g]),
                c='darkblue', label='Gradient')
        ax[1].set_ylabel(r'$|d\log L/d \log X|$')
        ax[1].set_yscale('log')

        ax[2].plot(it, self.likelihood_evaluations, c='darkblue', label='Evalutions')
        ax[2].set_ylabel('logL evaluations')

        ax[3].plot(it, self.logZ_history, label='logZ', c='darkblue')
        ax[3].set_ylabel('logZ')
        ax[3].legend(frameon=False)

        ax_dz = plt.twinx(ax[3])
        ax_dz.plot(it, self.dZ_history, label='dZ', c='lightblue')
        ax_dz.set_ylabel('dZ')
        ax_dz.legend(frameon=False)

        ax[4].plot(it, self.mean_acceptance_history, c='darkblue', label='Proposal')
        ax[4].plot(self.population_iterations, self.population_acceptance,
                c='lightblue', label='Population')
        ax[4].set_ylabel('Acceptance')
        ax[4].set_ylim((-0.1, 1.1))
        ax[4].legend(frameon=False)

        it = (np.arange(len(self.rolling_p))) * self.nlive
        ax[5].plot(it, self.rolling_p, c='darkblue', label='p-value')
        ax[5].set_ylabel('p-value')

        ax[-1].set_xlabel('Iteration')

        for t in self.training_iterations:
            for a in ax:
                a.axvline(t, ls='--', alpha=0.7, color='k')

        plt.tight_layout()

        fig.savefig(f'{self.output}/state.png')

    def update_state(self, force=False):
        """
        Update state after replacing a live point
        """

        if (pa := self.proposal.population_acceptance) is not None:
            self.population_acceptance.append(pa)
            self.population_iterations.append(self.iteration)

        if not (self.iteration % (self.nlive // 10)) or force:
            self.likelihood_evaluations.append(
                    self.model.likelihood_evaluations)
            self.min_likelihood.append(self.logLmin)
            self.max_likelihood.append(self.logLmax)
            self.logZ_history.append(self.state.logZ)
            self.dZ_history.append(self.condition)
            self.mean_acceptance_history.append(self.mean_acceptance)

        if not (self.iteration % self.nlive) or force:
            if not force:
                self.check_insertion_indices()
            if self.plot:
                if not force:
                    plot_indices(self.insertion_indices[-self.nlive:], self.nlive,
                            plot_breakdown=False,
                            filename=f'{self.output}/diagnostics/insertion_indices_{self.iteration}.png')
                self.plot_state()

            if self.uninformed_sampling:
                self.block_acceptance = 0.
                self.block_iteration = 0

    def checkpoint(self):
        """
        Checkpoint its internal state
        """
        logger.critical('Checkpointing nested sampling')
        safe_file_dump(self, self.resume_file, pickle, save_existing=True)

    def nested_sampling_loop(self, save=True):
        """
        main nested sampling loop
        """
        if not self.initialised:
            self.initialise()

        if self.prior_sampling:
            for i in range(self.nlive):
                self.nested_samples = self.params.copy()
                #self.nested_samples.append(self.params[i])
            if save:
                self.write_nested_samples_to_file()
                self.write_evidence_to_file()
            logger.warning("Nested Sampling process {0!s}, exiting".format(os.getpid()))
            return 0

        self.update_state()

        while self.condition > self.tolerance:

            self.check_state()

            self.consume_sample()

            self.update_state()

            if self.iteration >= self.max_iteration:
                break

        if self.proposal.pool is not None:
            self.proposal._close_pool()

        if self.store_live_points:
            np.savetxt(self.live_points_dir + '/replacement_live_points.dat',
                    self.replacement_points,
                    header='\t'.join(self.live_points.dtype.names))

        # final adjustments
        for i, p in enumerate(self.live_points):
            self.state.increment(p['logL'], nlive=self.nlive-i)
            self.nested_samples.append(p)

        # Refine evidence estimate
        self.update_state(force=True)
        self.state.finalise()
        self.logZ = self.state.logZ
        self.likelihood_calls = self.model.likelihood_evaluations
        # output the chain and evidence
        if save:
            self.write_nested_samples_to_file()
            self.write_evidence_to_file()

        logger.critical('Final evidence: {0:0.2f}'.format(self.state.logZ))
        logger.critical('Information: {0:.2f}'.format(self.state.info))

        self.check_insertion_indices(rolling=False)

        if self.checkpointing:
            self.checkpoint()

        logger.info(('Total training time: '
            f'{datetime.timedelta(seconds=self.training_time)}'))
        logger.info(f'Total likelihood evaluations: {self.likelihood_calls:3d}')

        return self.state.logZ, np.array(self.nested_samples)

    @classmethod
    def resume(cls, filename, model, flow_config={}, weights_file=None):
        """
        Resumes the interrupted state from a
        checkpoint pickle file.
        """
        logger.critical('Resuming NestedSampler from ' + filename)
        with open(filename,"rb") as f:
            obj = pickle.load(f)
        model.likelihood_evaluations += obj.likelihood_evaluations[-1]
        obj.model = model
        obj._uninformed_proposal.model = model
        obj._flow_proposal.model = model
        obj._flow_proposal.flow_config = flow_config
        obj._flow_proposal.pool = None
        if (m := obj._flow_proposal.mask) is not None:
            if isinstance(m, list):
                m = np.array(m)
            obj._flow_proposal.flow_config['model_config']['kwargs']['mask'] = m
        obj._flow_proposal.initialise()
        if weights_file is None:
            weights_file = obj._flow_proposal.weights_file
        obj._flow_proposal.flow.reload_weights(weights_file)
        obj.resumed = True
        return obj

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['model']
        return state

    def __setstate__(self, state):
        self.__dict__ = state
