import logging
import time
from typing import Any, Dict, Iterable, List, Optional

import sacrebleu  # type: ignore
import torch
import torcheval
import torcheval.metrics.toolkit
import torchtnt.utils.distributed
from sacrebleu.metrics.base import Metric as SacrebleuMetric  # type: ignore
from torch import Tensor

log = logging.getLogger(__name__)


class Metrics(Dict[str, Any]):
    def compute(self, prefix: str = "", sync: bool = False) -> Dict[str, float]:
        results = {}
        # Don't sync if single worker to avoid warning
        sync = sync and torchtnt.utils.distributed.get_world_size() > 1
        for k, v in self.items():
            if not isinstance(v, torcheval.metrics.Metric):
                val = v
            elif sync:
                # TODO: allow histograms
                val = torcheval.metrics.toolkit.sync_and_compute(v)
                # only rank 0 will get the actual value
                val = val.item() if val is not None else None  # type: ignore
            else:
                val = v.compute().item()
            results[prefix + k] = val
        return results

    def reset(self) -> None:
        for k, v in self.items():
            if k.endswith("_min"):
                # global minumum counters are never resetted.
                continue
            if isinstance(v, torcheval.metrics.Metric):
                v.reset()


class CounterBasedMetric(torcheval.metrics.Metric[Tensor]):
    @torch.inference_mode()
    def merge_state(
        self, metrics: Iterable["CounterBasedMetric"]
    ) -> "CounterBasedMetric":
        for k in self._state_name_to_default:
            x = getattr(self, k)
            for metric in metrics:
                x += getattr(metric, k)
        return self


class Bleu(CounterBasedMetric):
    """Bleu metric, based on string generated by the model.

    Backed by sacrebleu. Can compute any metric supported by sacrebleu:
    BLEU, ChrF, ChrF++, sp-BLEU...
    """

    bleu_counts: Tensor
    num_refs: Tensor

    def __init__(
        self,
        metric: Optional[SacrebleuMetric] = None,
        *,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__(device=device)
        self._metric = sacrebleu.BLEU() if metric is None else metric  # type: ignore
        size = self._determine_size()
        self._add_state(
            "bleu_counts",
            torch.zeros(size, dtype=torch.int64, device=self.device),
        )
        self._add_state(
            "num_refs",
            torch.zeros((1,), dtype=torch.int64, device=self.device),
        )

    def _determine_size(self) -> int:
        ref_info = self._metric._extract_reference_info(["hel lo"])
        counts = self._metric._compute_segment_statistics("he hel llo", ref_info)
        return len(counts)

    @torch.inference_mode()
    def update(self, hypothesis: str, references: List[str]) -> None:  # type: ignore[override]
        # We may receive a CString here.
        hypothesis = str(hypothesis)
        references = [str(ref) for ref in references]
        ref_info = self._metric._extract_reference_info(references)
        counts = self._metric._compute_segment_statistics(hypothesis, ref_info)
        self.num_refs += len(references)
        self.bleu_counts += torch.tensor(
            counts, dtype=self.bleu_counts.dtype, device=self.device
        )

    def compute(self) -> torch.Tensor:
        bleu = self._metric._compute_score_from_stats(self.bleu_counts.tolist())
        self._metric.num_refs = self.num_refs.item()
        signature = self._metric.get_signature()
        log.info(f"{signature} score: {bleu}")
        return torch.tensor(bleu.score)


class Perplexity(torcheval.metrics.Metric[Tensor]):
    ...


class EffectiveThroughput(CounterBasedMetric):
    """
    Calculates the throughput value which is the number of elements processed per second.

    The difference to torcheval.metrics.Throughput is that it measures the time between two "compute" calls.
    """

    num_total: Tensor
    start_time: Tensor

    def __init__(self, *, device: Optional[torch.device] = None) -> None:
        super().__init__(device=device)
        self._add_state("num_total", torch.tensor(0.0, device=self.device))
        self._add_state(
            "start_time",
            torch.tensor(time.perf_counter(), device=self.device),
        )

    @torch.inference_mode()
    def update(self, num_processed: int) -> "EffectiveThroughput":
        """
        Update states with the values and weights.
        Args:
            num_processed: Number of items processed
        """
        if num_processed < 0:
            raise ValueError(
                f"Expected num_processed to be a non-negative number, but received {num_processed}."
            )
        self.num_total += num_processed  # type: ignore
        return self

    @torch.inference_mode()
    def compute(self) -> Tensor:
        elapsed = -self.start_time + time.perf_counter()
        throughput = self.num_total / elapsed
        return throughput

    @torch.inference_mode()
    def merge_state(
        self, metrics: Iterable["EffectiveThroughput"]  # type: ignore[override]
    ) -> "EffectiveThroughput":
        for metric in metrics:
            self.num_total += metric.num_total.to(self.device)
            # this assumes the metric is used within a fully-synchronous program.
            # In this scenario, the slowest process becomes the bottleneck for the
            # program's execution. As a result, we use the max, as the overall throughput
            # is gated based on the rank that takes the longest to complete.
            # TODO: should this be configurable?
            self.start_time = torch.min(
                self.start_time, metric.start_time.to(self.device)
            )
        return self

    def reset(self) -> "EffectiveThroughput":
        self.start_time.fill_(time.perf_counter())
        return self


class WER(CounterBasedMetric):
    KEYS = ["hits", "substitutions", "deletions", "insertions"]
    counters: Tensor

    def __init__(self, device: Optional[torch.device] = None):
        super().__init__()
        self._add_state(
            "counters",
            torch.zeros((len(self.KEYS),), dtype=torch.int32, device=device),
        )

    def update(self, prediction: str, reference: str) -> None:  # type: ignore[override]
        # TODO: Could we avoid going through a dict ?
        # TODO: do this without jiwer
        import jiwer  # type: ignore

        measures = jiwer.compute_measures(reference, prediction)
        self.counters += torch.tensor([measures[k] for k in self.KEYS])

    def compute(self) -> Tensor:
        counters = self.counters.detach().cpu()
        measures = {k: counters[i] for i, k in enumerate(self.KEYS)}
        incorrect = (
            measures["substitutions"] + measures["deletions"] + measures["insertions"]
        )
        total = measures["substitutions"] + measures["deletions"] + measures["hits"]
        return incorrect / total
