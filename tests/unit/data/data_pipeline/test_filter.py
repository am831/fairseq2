# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import pytest

from fairseq2.data import read_sequence


class TestFilterOp:
    def test_op_works_as_expected(self) -> None:
        def fn(d: int) -> bool:
            return d % 2 == 1

        dp = read_sequence([1, 2, 3, 4, 5, 6, 7, 8, 9]).filter(fn).and_return()

        for _ in range(2):
            assert list(dp) == [1, 3, 5, 7, 9]

            dp.reset()

    def test_op_propagates_errors_as_expected(self) -> None:
        def fn(d: int) -> bool:
            if d == 3:
                raise ValueError("filter error")

            return True

        dp = read_sequence([1, 2, 3, 4]).filter(fn).and_return()

        with pytest.raises(ValueError, match=r"^filter error$"):
            for d in dp:
                pass
