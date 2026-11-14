import unittest
from datetime import date
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from radar import build_boards, categorize, render_html


def repo(stars, name='org/ai-tool', created='2026-01-01T00:00:00Z'):
    return dict(full_name=name, url='https://github.com/' + name,
                description='AI agent tool', stars=stars, forks=12, open_issues=3,
                created_at=created, pushed_at='2026-11-13T00:00:00Z',
                archived=False, categories=['AI Agent'], topics=['ai-agent'])


class RankingTests(unittest.TestCase):
    def test_exact_week_month_star_delta(self):
        current = dict(date='2026-11-14', repositories=[repo(1400)])
        week = dict(date='2026-11-07', repositories=[repo(900)])
        month = dict(date='2026-10-15', repositories=[repo(400)])
        boards = build_boards(current, week, month)
        self.assertEqual(boards['week'][0]['delta7'], 500)
        self.assertEqual(boards['month'][0]['delta30'], 1000)

    def test_never_invent_missing_historical_growth(self):
        curr = dict(date='2026-11-14', repositories=[repo(2200)])
        boards = build_boards(curr, None, None)
        self.assertEqual(boards['week'], [])
        self.assertEqual(boards['month'], [])
        self.assertIsNone(boards['established'][0]['delta7'])

    def test_requires_matching_tracked_repo(self):
        curr = dict(date='2026-11-14', repositories=[repo(2000)])
        old = dict(date='2026-11-07', repositories=[repo(100, 'other/repo')])
        self.assertFalse(build_boards(curr, old, None)['week'])

    def test_render_escapes_untrusted_description(self):
        r = repo(4000)
        r['description'] = '<script>alert(1)</script>'
        report = dict(date='2026-11-14', repository_count=1, baseline7=False,
                      baseline30=False, search_failures=[], boards={
                          'week': [], 'month': [], 'established': [dict(r,delta7=None,delta30=None)], 'potential': []})
        text = render_html(report)
        self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('<script>alert(1)</script>', text)

    def test_topic_categorization(self):
        self.assertIn('MCP', categorize({'name': 'test', 'topics': ['model-context-protocol']}))


if __name__ == '__main__':
    unittest.main()
