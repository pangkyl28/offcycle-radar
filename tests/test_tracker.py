import json
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile

import tracker

CFG = {"target_years": [2027, 2028], "include_possible_offcycle": True, "skip_phd_only": True}


class ParsingTests(unittest.TestCase):
    def test_general_github_parsing_closed_rows_and_continuation(self):
        md = """# Header
| Company | Role | Location | Apply | Added |
| --- | --- | --- | --- | --- |
| Alpha | Software Engineer Intern (Fall 2027) | Chicago, IL | [apply](https://example.com/alpha) | 2026-11-08 |
| ↳ | Software Engineer Intern (Spring 2027) | NYC | <a href=\"https://example.com/beta\">Apply</a> | 2026-11-07 |
| Beta | Software Intern | New York, NY | 🔒 | - |
"""
        results = tracker.parse_general_github({"name": "Curated"}, md)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1]["company"], "Alpha")
        self.assertEqual(results[1]["url"], "https://example.com/beta")

    def test_quant_only_internship_sections(self):
        md = """## Internships
### Trading — internships
| Company | Role | Location | Focus | Salary | Posted |
|---|---|---|---|---|---|
| **[Jump Trading](https://example.com/jump)** | [Quantitative Researcher (Off-Cycle Intern)](https://example.com/1) | Chicago | Quant | — | Nov 8 |
| ↳ | [Campus Quant Researcher (Spring 2027 Intern)](https://example.com/2) | New York | Quant | — | Nov 8 |
## Graduate roles
### Trading — graduate roles
| Jump Trading | [Quantitative Trader Intern (Spring 2027)](https://example.com/3) | Chicago | — | — | Nov 8 |
"""
        results = tracker.parse_quant_github({"name": "QuantScraper"}, md)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1]["company"], "Jump Trading")
        self.assertEqual(results[0]["hint"], "quant")

    def test_valid_link_requires_https(self):
        self.assertFalse(tracker.valid_link("javascript:alert(1)"))
        self.assertTrue(tracker.valid_link("https://example.com/apply"))
        self.assertFalse(tracker.valid_link("http://example.com"))


class FilterTests(unittest.TestCase):
    def job(self, title, desc="", hint="", company="X"):
        return tracker.make_job(company, title, "Chicago, IL", "https://example.com/123", "Test", desc, hint)

    def test_2027_spring_swe(self):
        result = tracker.classify(self.job("Software Engineer Intern (Spring 2027)"), CFG)
        self.assertEqual(result["track"], "SWE")
        self.assertEqual(result["term"], "Spring 2027")
        self.assertEqual(result["confidence"], "Confirmed")

    def test_offcycle_quant(self):
        result = tracker.classify(self.job("Campus Quantitative Researcher (Off-Cycle Intern)"), CFG)
        self.assertEqual(result["track"], "Quant")
        self.assertEqual(result["term"], "Off-cycle")

    def test_description_season(self):
        result = tracker.classify(self.job("Software Developer Intern", "Applications for winter 2027."), CFG)
        self.assertEqual(result["term"], "Winter 2027")

    def test_past_year_in_description_excluded(self):
        self.assertIsNone(tracker.classify(self.job("Software Engineer Intern", "This is a Spring 2026 internship"), CFG))

    def test_preserve_job_id_in_canonical_url(self):
        a = "https://tower.com/open-positions/?gh_jid=123&utm_source=alert"
        b = "https://tower.com/open-positions/?gh_jid=456&utm_source=alert"
        self.assertNotEqual(tracker.canonical_url(a), tracker.canonical_url(b))

    def test_explicit_summer_excluded(self):
        self.assertIsNone(tracker.classify(self.job("Software Engineer Intern (Summer 2027)", "Spring opportunities available"), CFG))

    def test_past_year_excluded(self):
        self.assertIsNone(tracker.classify(self.job("Software Engineer Co-op (Fall 2026)"), CFG))

    def test_phd_only_excluded(self):
        self.assertIsNone(tracker.classify(self.job("Quantitative Research Intern (PhD, Spring 2027)"), CFG))

    def test_possible_unconfirmed(self):
        result = tracker.classify(self.job("Software Engineer Intern"), CFG)
        self.assertEqual(result["confidence"], "Possible")

    def test_coop(self):
        result = tracker.classify(self.job("Platform Engineer Co-op (2027)"), CFG)
        self.assertEqual(result["term"], "Co-op")
        self.assertEqual(result["track"], "SWE")

    def test_unrelated_excluded(self):
        self.assertIsNone(tracker.classify(self.job("Product Manager Intern (Fall 2027)"), CFG))
        self.assertIsNone(tracker.classify(self.job("Quant Researcher, New Grad 2027"), CFG))


class PipelineTests(unittest.TestCase):
    def test_new_job_detection_and_first_run_no_alert(self):
        cfg = {**CFG, "sources": [{"type": "github_general", "name": "A", "url": "https://example.com/jobs"}],
               "notify_possible_offcycle": False, "max_notifications_per_run": 25}
        first = tracker.make_job("Alpha", "Software Engineer Intern (Spring 2027)", "Chicago, IL", "https://example.com/apply", "A")
        next_job = tracker.make_job("Beta", "Quant Trader Intern (Fall 2027)", "New York, NY", "https://example.com/quant", "A")
        with tempfile.TemporaryDirectory() as d:
            state_path = Path(d) / "state.json"
            out_path = Path(d) / "jobs.json"
            with patch.object(tracker, 'CONFIG_PATH', Path(d)/'config.json'), \
                 patch.object(tracker, 'STATE_PATH', state_path), \
                 patch.object(tracker, 'OUTPUT_PATH', out_path), \
                 patch.object(tracker, 'fetch_source', side_effect=[[first], [first, next_job], [first, next_job]]), \
                 patch.object(tracker, 'deliver_notifications', return_value=['Telegram']) as notify:
                (Path(d)/'config.json').write_text(json.dumps(cfg))
                results1 = tracker.scan(session=object())
                self.assertEqual(len(results1['alert_candidates']), 0)
                self.assertEqual(len(results1['jobs']), 1)
                self.assertFalse(notify.called)
                results2 = tracker.scan(session=object())
                self.assertEqual(len(results2['alert_candidates']), 1)
                self.assertEqual(results2['alert_candidates'][0]['company'], 'Beta')
                self.assertTrue(notify.called)
                results3 = tracker.scan(session=object())
                self.assertEqual(len(results3['alert_candidates']), 0)
                self.assertEqual(json.loads(out_path.read_text())['summary']['total'], 2)

    def test_possible_role_later_confirmed_triggers_alert(self):
        cfg = {**CFG, "sources": [{"type": "github_general", "name": "A", "url": "https://example.com/jobs"}]}
        possible = tracker.make_job("Alpha", "Software Engineer Intern", "Chicago, IL", "https://example.com/apply", "A")
        confirmed = {**possible, "description": "Hiring for Spring 2027"}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.json'
            p.write_text(json.dumps(cfg))
            with patch.object(tracker, 'CONFIG_PATH', p), \
                 patch.object(tracker, 'STATE_PATH', Path(d)/'state.json'), \
                 patch.object(tracker, 'OUTPUT_PATH', Path(d)/'jobs.json'), \
                 patch.object(tracker, 'fetch_source', side_effect=[[possible], [confirmed]]), \
                 patch.object(tracker, 'deliver_notifications', return_value=['Telegram']):
                self.assertEqual(len(tracker.scan(session=object())['alert_candidates']), 0)
                second = tracker.scan(session=object())
                self.assertEqual(len(second['new_jobs']), 0)
                self.assertEqual(len(second['alert_candidates']), 1)

    def test_notification_cap_defers_extra_matches(self):
        cfg = {**CFG, "sources": [{"type": "github_general", "name": "A", "url": "https://example.com/jobs"}],
               "max_notifications_per_run": 1}
        jobs = [tracker.make_job(str(i), "Software Engineer Intern (Fall 2027)", "Chicago, IL", f"https://example.com/{i}", "A") for i in (1, 2)]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.json'
            p.write_text(json.dumps(cfg))
            with patch.object(tracker, 'CONFIG_PATH', p), \
                 patch.object(tracker, 'STATE_PATH', Path(d)/'state.json'), \
                 patch.object(tracker, 'OUTPUT_PATH', Path(d)/'jobs.json'), \
                 patch.object(tracker, 'fetch_source', side_effect=[[], jobs, jobs, jobs]), \
                 patch.object(tracker, 'deliver_notifications', return_value=['Telegram']):
                tracker.scan(session=object())
                self.assertEqual(len(tracker.scan(session=object())['alert_candidates']), 1)
                self.assertEqual(len(tracker.scan(session=object())['alert_candidates']), 1)
                self.assertEqual(len(tracker.scan(session=object())['alert_candidates']), 0)

    def test_all_sources_failed_does_not_mutate(self):
        cfg = {**CFG, "sources": [{"type": "github_general", "name": "A", "url": "https://example.com/jobs"}]}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.json'
            p.write_text(json.dumps(cfg))
            with patch.object(tracker, 'CONFIG_PATH', p), \
                 patch.object(tracker, 'STATE_PATH', Path(d)/'state.json'), \
                 patch.object(tracker, 'OUTPUT_PATH', Path(d)/'jobs.json'), \
                 patch.object(tracker, 'fetch_source', side_effect=ValueError('offline')):
                with self.assertRaises(RuntimeError):
                    tracker.scan(session=object())
                self.assertFalse((Path(d)/'state.json').exists())
                self.assertFalse((Path(d)/'jobs.json').exists())

if __name__ == '__main__':
    unittest.main()
