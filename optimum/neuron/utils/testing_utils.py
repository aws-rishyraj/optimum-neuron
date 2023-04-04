# coding=utf-8
# Copyright 2023 HuggingFace Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities for tests."""


def is_trainium_test(test_case):
    try:
        import pytest
    except ImportError:
        return test_case
    else:
        return pytest.mark.is_trainium_test()(test_case)


def is_inferentia_test(test_case):
    try:
        import pytest
    except ImportError:
        return test_case
    else:
        return pytest.mark.is_inferentia_test()(test_case)
