import pytest
import time

import numpy as np

from ase.atoms import Atoms
from ase.calculators.emt import EMT

from wfl.configset import ConfigSet, OutputSpec
from wfl.generate import buildcell
from wfl.calculators import generic
from wfl.autoparallelize.autoparainfo import AutoparaInfo


def test_empty_iterator(tmp_path):
    co = buildcell.run(range(0), OutputSpec(tmp_path / 'dummy.xyz'),  buildcell_cmd='dummy', buildcell_input='dummy')

    assert len([at for at in co]) == 0


def test_autopara_info_dict():
    np.random.seed(5)

    ats = []
    nconf = 60
    for _ in range(nconf):
        ats.append(Atoms(['Al'] * nconf, scaled_positions=np.random.uniform(size=(nconf, 3)), cell=[10, 10, 10], pbc=[True] * 3))

    co = generic.run(ConfigSet(ats), OutputSpec(), EMT(), output_prefix="_auto_", autopara_info={"num_python_subprocesses": 1})
    assert len(list(co)) == nconf


@pytest.mark.perf
def test_pool_speedup():
    np.random.seed(5)

    ats = []
    nconf = 60
    for _ in range(nconf):
        ats.append(Atoms(['Al'] * nconf, scaled_positions=np.random.uniform(size=(nconf, 3)), cell=[10, 10, 10], pbc=[True] * 3))

    t0 = time.time()
    co = generic.run(ConfigSet(ats), OutputSpec(), EMT(), output_prefix="_auto_", autopara_info=AutoparaInfo(num_python_subprocesses=1))
    dt_1 = time.time() - t0

    t0 = time.time()
    co = generic.run(ConfigSet(ats), OutputSpec(), EMT(), output_prefix="_auto_", autopara_info=AutoparaInfo(num_python_subprocesses=2))
    dt_2 = time.time() - t0

    print("time ratio", dt_2 / dt_1)
    assert dt_2 < dt_1 * (2/3)
