"""Versions of the liquidity-incentive study, kept apart from the maker study's.

The maker study's recorder is ``STRATEGY_VERSION`` 1.1.0 and nothing here touches it.
The LIP recorder and the LIP simulator version separately: a fix to how credit is
scored is not a change to what was recorded, and each is pinned by the
pre-registration (docs/lip-preregistration.md). Data from different versions is
never pooled.
"""

LIP_RECORDER_VERSION = "lip-1.0.0"
LIP_SIM_VERSION = "lip-sim-1.0.0"
#: Salt of the admission hash (pre-registration section 2.4). A plain fixed string, so
#: it cannot have been searched for.
SAMPLE_SALT = "pmvl-lip-1.0.0"
