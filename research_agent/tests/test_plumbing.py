"""Registry provenance, config parsing, CTC parsing, and the real encoder."""

import os
import tempfile
import unittest

from av2ra.core import ids
from av2ra.core.ledger import Decision, Ledger
from av2ra.core.models import (
    Experiment,
    Hypothesis,
    Interval,
    MechanismClass,
    ScreenSummary,
    Tier,
    TierResult,
    Verdict,
)
from av2ra.core.registry import MeasurementRow, Registry
from av2ra.ctc.contract import frames_for
from av2ra.ctc.eda import EdaBackend, EdaConfig, parse_comparison
from av2ra.util import yamlish

BUILD = "/tmp/claude-0/-home-user-avm/b9c652ff-af93-5fcc-912c-9ef4312a7d5c/scratchpad/avmbuild"
CLIPS = "/tmp/claude-0/-home-user-avm/b9c652ff-af93-5fcc-912c-9ef4312a7d5c/scratchpad/clips"


def experiment(identifier="N0001"):
  hypothesis = Hypothesis(
      id=identifier, title="t", subsystem="transform",
      mechanism=MechanismClass.APPROX, lens="profile_hotspot",
      statement="s", rationale="r", target_presets=[4],
  )
  out = Experiment(id=identifier, hypothesis=hypothesis, base_sha="abc123def456")
  out.apply_tier_result(
      TierResult(
          tier=Tier.T3_SCREEN, passed=True, verdict=Verdict.SCREEN_PASS, ratio=26.7,
          summary=ScreenSummary(
              preset=4, clip_set="screen",
              speed_delta=Interval(-4.0, -4.5, -3.5),
              bdrate_by_metric={"PSNR-YUV": Interval(0.15, 0.10, 0.20)},
          ),
      )
  )
  return out


class RegistryTest(unittest.TestCase):

  def setUp(self):
    self.registry = Registry(os.path.join(tempfile.mkdtemp(), "r.sqlite"))

  def test_round_trips_a_full_experiment(self):
    self.registry.upsert(experiment())
    loaded = self.registry.get("N0001")
    self.assertEqual(loaded.verdict, Verdict.SCREEN_PASS)
    self.assertAlmostEqual(loaded.tiers[0].summary.speed_delta.point, -4.0)

  def test_disagreeing_measurements_surface_instead_of_overwriting(self):
    """The corpus ended up with two different CTC numbers for the same patches."""
    self.registry.upsert(experiment())
    common = dict(
        experiment_id="N0001", tier="T5_ctc", class_name="A1", preset=4,
        metric="PSNR-YUV", base_sha="abc123def456",
        metric_def="bdrate/pchip/yuv-14-1-1",
    )
    self.registry.record_measurement(MeasurementRow(value=0.15, backend="eda", job_id="u1", **common))
    self.registry.record_measurement(MeasurementRow(value=0.40, backend="eda", job_id="u2", **common))
    conflicts = self.registry.conflicting_measurements()
    self.assertEqual(len(conflicts), 1)
    self.assertEqual(len(conflicts[0]["rows"]), 2)

  def test_a_moved_anchor_invalidates_its_measurements(self):
    self.registry.upsert(experiment())
    self.registry.record_measurement(
        MeasurementRow(
            experiment_id="N0001", tier="T3_screen", class_name="screen", preset=4,
            metric="instructions", value=-4.0, base_sha="abc123def456",
        )
    )
    self.assertEqual(self.registry.mark_stale(base_sha="abc123def456", reason="moved"), 1)
    self.assertEqual(self.registry.measurements("N0001"), [])
    self.assertEqual(len(self.registry.measurements("N0001", include_stale=True)), 1)

  def test_csv_export_records_the_anchor(self):
    self.registry.upsert(experiment())
    path = self.registry.export_csv(os.path.join(tempfile.mkdtemp(), "registry.csv"))
    with open(path, encoding="utf-8") as handle:
      text = handle.read()
    self.assertIn("scoped to its base_sha", text)
    self.assertIn("abc123def4", text)


class LedgerTest(unittest.TestCase):

  def test_retractions_are_marked_not_erased(self):
    root = tempfile.mkdtemp()
    ledger = Ledger(root)
    first = ledger.append(
        Decision("N0001", "kept", "a conclusion", lessons=["gates never deliver"])
    )
    ledger.append(Decision("N0002", "retracted", "that rested on inert arms", retracts=first))
    with open(os.path.join(root, "DECISIONS.md"), encoding="utf-8") as handle:
      text = handle.read()
    self.assertIn("[RETRACTED]", text)
    self.assertIn("gates never deliver", ledger.lessons())


class IdsTest(unittest.TestCase):

  def test_variants_stay_in_one_family(self):
    self.assertEqual(ids.variant_id("N0042", 0), "N0042a")
    self.assertEqual(ids.family_of("N0042c"), "N0042")
    self.assertEqual(ids.next_counter(["N0001", "N0042c", "not-an-id"]), 43)


class YamlishTest(unittest.TestCase):

  def test_fallback_parser_agrees_with_pyyaml_on_the_shipped_configs(self):
    try:
      import yaml
    except ImportError:
      self.skipTest("PyYAML not installed; the fallback is the only parser here")
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
    files = [os.path.join(root, "default.yaml")] + [
        os.path.join(root, "sites", name) for name in sorted(os.listdir(os.path.join(root, "sites")))
    ]
    original = yamlish._pyyaml
    try:
      for path in files:
        with open(path, encoding="utf-8") as handle:
          text = handle.read()
        yamlish._pyyaml = None
        mine = yamlish.loads(text)
        yamlish._pyyaml = original
        self.assertEqual(mine, yaml.safe_load(text), path)
    finally:
      yamlish._pyyaml = original


class EdaTest(unittest.TestCase):

  TABLE = """| Sequence (RA, A1) | PSNR-Y | PSNR-YUV | EncTime | DecTime |
| :--- | :---: | :---: | :---: | :---: |
| BoxingPractice_3840x2160 | +0.10% | +0.12% | **99.03%** | 98.95% |
| Crosswalk_3840x2160 | +0.20% | +0.22% | **95.98%** | 99.70% |
| **Average (RA, A1)** | **+0.15%** | **+0.17%** | **97.50%** | **99.05%** |
"""

  def test_parses_the_reported_table_shape(self):
    result = parse_comparison(self.TABLE, testset="a1", preset=4)
    self.assertEqual(len(result.sequences), 2)
    self.assertAlmostEqual(result.average_bdrate["PSNR-YUV"], 0.17)
    self.assertAlmostEqual(result.speedup_pct, 2.5)

  def test_parses_the_csv_shape(self):
    text = "sequence,PSNR-Y,PSNR-YUV,EncTime\nclipA,0.10,0.12,99.03\nclipB,0.20,0.22,95.98\n"
    result = parse_comparison(text, testset="a2", preset=2)
    self.assertEqual(len(result.sequences), 2)

  def test_frame_counts_are_per_class(self):
    """A single --frame_count cannot express A1=17 and A2=33."""
    self.assertEqual(frames_for("a1"), 17)
    self.assertEqual(frames_for("a2"), 33)
    backend = EdaBackend(
        EdaConfig(kickoff_script="/x/k.sh", compare_script="/x/c.py",
                  state_dir=tempfile.mkdtemp())
    )
    a1 = backend.build_kickoff_argv(tag="t", worktree="/w", preset=4, testset="a1", config="ra")
    a2 = backend.build_kickoff_argv(tag="t", worktree="/w", preset=4, testset="a2", config="ra")
    self.assertIn("--frame_count=17", a1)
    self.assertIn("--frame_count=33", a2)

  def test_a_run_without_a_question_is_refused(self):
    from av2ra.core.models import CtcRequest

    backend = EdaBackend(
        EdaConfig(kickoff_script="/x/k.sh", compare_script="/x/c.py",
                  state_dir=tempfile.mkdtemp())
    )
    result = backend.submit(CtcRequest(experiment_id="N1", base_sha="abc", patch_ref="/w"))
    self.assertEqual(result.state, "failed")
    self.assertIn("no question recorded", result.error)


class RealEncoderTest(unittest.TestCase):
  """Against the actual avmenc, when one has been built."""

  def setUp(self):
    self.encoder = os.path.join(BUILD, "avmenc")
    self.decoder = os.path.join(BUILD, "avmdec")
    self.clip = os.path.join(CLIPS, "synth_64x64_30.y4m")
    if not (os.path.exists(self.encoder) and os.path.exists(self.clip)):
      self.skipTest("no local avmenc build")

  def test_the_binary_is_the_one_the_protocol_assumes(self):
    from av2ra.measure.encode import probe_encoder

    probe = probe_encoder(self.encoder)
    self.assertTrue(probe["has_qp"])
    # CTC 2.0+ passes --qp. The draft screening command used --cq-level, which
    # this encoder does not accept at all.
    self.assertFalse(probe["has_cq_level"])

  def test_a_real_encode_parses_and_decodes(self):
    from av2ra.measure.encode import (
        ClipSpec, EncodeConfig, EncodeJob, decode_verify, run_encode,
    )

    out = os.path.join(tempfile.mkdtemp(), "e.obu")
    clip = ClipSpec(
        name="synth64", path=self.clip, width=64, height=64, fps_num=30,
        bit_depth=8, file_class="A3",
    )
    job = EncodeJob(
        clip=clip, cfg=EncodeConfig(preset=9, qp=185, frames=2), arm="anchor",
        encoder=self.encoder, out_path=out,
    )
    result = run_encode(job)
    self.assertTrue(result.ok, result.error)
    self.assertGreater(result.bitrate_kbps, 0.0)
    self.assertGreater(result.psnr_y, 0.0)
    self.assertGreater(result.cx_time_s, 0.0)
    self.assertEqual(len(result.bitstream_md5), 32)
    ok, decoded_md5, message = decode_verify(self.decoder, out)
    self.assertTrue(ok, message)
    self.assertEqual(len(decoded_md5), 32)

  def test_the_encoder_is_deterministic_for_a_fixed_configuration(self):
    from av2ra.measure.encode import ClipSpec, EncodeConfig, EncodeJob, run_encode

    directory = tempfile.mkdtemp()
    clip = ClipSpec(
        name="synth64", path=self.clip, width=64, height=64, fps_num=30,
        bit_depth=8, file_class="A3",
    )
    digests = set()
    for index in range(2):
      job = EncodeJob(
          clip=clip, cfg=EncodeConfig(preset=9, qp=185, frames=2), arm="anchor",
          encoder=self.encoder, out_path=os.path.join(directory, f"e{index}.obu"),
          rep=index,
      )
      digests.add(run_encode(job).bitstream_md5)
    self.assertEqual(len(digests), 1, "the encoder produced two different bitstreams")


if __name__ == "__main__":
  unittest.main()
