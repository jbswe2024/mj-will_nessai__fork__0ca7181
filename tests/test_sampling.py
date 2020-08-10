import numpy as np
import pytest

from flowproposal.flowsampler import FlowSampler

def test_sampling_with_rescale(model, flow_config, tmpdir):
    output = str(tmpdir.mkdir('w_rescale'))
    fp = FlowSampler(model, output=output, resume=False, nlive=100, plot=False,
        flow_config=flow_config, training_frequency=10, maximum_uninformed=9,
        rescale_parameters=True, seed=1234, max_iteration=11)
    fp.run()
    assert fp.ns.proposal.flow.weights_file is not None

def test_sampling_without_rescale(model, flow_config, tmpdir):
    output = str(tmpdir.mkdir('wo_rescale'))
    fp = FlowSampler(model, output=output, resume=False, nlive=100, plot=False,
        flow_config=flow_config, training_frequency=10, maximum_uninformed=9,
        rescale_parameters=False, seed=1234, max_iteration=11)
    fp.run()
    assert fp.ns.proposal.flow.weights_file is not None
