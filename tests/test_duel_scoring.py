"""Проверки игровых правил без Telegram, БД и сторонних библиотек."""
import unittest

from duel_scoring import TaskScore as Task, calculate_result


class DuelScoringTests(unittest.TestCase):
    def test_points_take_priority_over_time(self):
        result = calculate_result({1: [Task(3, 119_000)], 2: [Task(2, 1000)]})
        self.assertEqual((result['winner_id'], result['decided_by']), (1, 'points'))

    def test_equal_points_compare_whole_seconds(self):
        result = calculate_result({1: [Task(3, 12_900)], 2: [Task(3, 11_999)]})
        self.assertEqual((result['winner_id'], result['decided_by']), (2, 'time'))

    def test_fractional_seconds_do_not_break_draw(self):
        result = calculate_result({1: [Task(3, 12_001)], 2: [Task(3, 12_999)]})
        self.assertIsNone(result['winner_id'])
        self.assertEqual(result['decided_by'], 'draw')

    def test_sum_before_discarding_fraction(self):
        result = calculate_result({1: [Task(1, 1900), Task(1, 1900)],
                                   2: [Task(1, 1500), Task(1, 1500)]})
        self.assertEqual(result['players'][1]['seconds'], 3)
        self.assertEqual(result['decided_by'], 'draw')

    def test_each_task_time_is_capped(self):
        result = calculate_result({1: [Task(0, 500_000), Task(0, 1000)],
                                   2: [Task(0, 120_000), Task(0, 1000)]})
        self.assertEqual(result['players'][1]['seconds'], 121)
        self.assertEqual(result['decided_by'], 'draw')

    def test_unfinished_game_has_no_result_before_deadline(self):
        self.assertEqual(calculate_result({1: [Task(3)], 2: [Task()]}), {'status': 'active'})

    def test_timeout_penalizes_partial_and_unopened_tasks(self):
        result = calculate_result({1: [Task(None, 1000, 1), Task()],
                                   2: [Task(0, 1000), Task(0, 1000)]}, expired=True)
        self.assertEqual(result['players'][1], {'points': 0, 'seconds': 240})
        self.assertEqual(result['winner_id'], 2)
        self.assertEqual(result['reason'], 'timeout')

    def test_opening_without_answer_does_not_count_as_play(self):
        result = calculate_result({1: [Task(None, 5000)], 2: [Task()]}, expired=True)
        self.assertEqual(result, {'status': 'cancelled', 'reason': 'no_answers'})

    def test_wrong_attempt_counts_as_play(self):
        result = calculate_result({1: [Task(attempts=1)], 2: [Task()]}, expired=True)
        self.assertEqual(result['status'], 'finished')
        self.assertEqual(result['decided_by'], 'draw')

    def test_invalid_task_sets_are_rejected(self):
        for players in ({1: [], 2: []}, {1: [Task()]}, {1: [Task()], 2: []}):
            with self.subTest(players=players), self.assertRaises(ValueError):
                calculate_result(players)


if __name__ == '__main__':
    unittest.main()
