import sys
import os
import unittest

# Добавляем родительскую директорию KD Connect в sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from combat_state_machine import CombatStateMachine, TestClock


class TestCombatStateMachine(unittest.TestCase):
    def setUp(self):
        self.clock = TestClock(initial_monotonic=1000.0, initial_time=1700000000.0)
        self.events = []
        self.session_starts = []
        self.session_ends = []

        self.fsm = CombatStateMachine(
            on_session_start=lambda m, mid: self.session_starts.append((m, mid)),
            on_session_end=lambda stats: self.session_ends.append(stats),
            on_event=lambda ev_type, count, stats: self.events.append((ev_type, count, dict(stats))),
            clock=self.clock
        )

    def _start_standard_match(self, map_name="levels/colorado", ip="195.133.60.10:20000", log_id="MISSION_LOG_001"):
        self.fsm.feed_game_log_line(f"Connection requested to: {ip}")
        self.fsm.feed_game_log_line(f"starting mission with LogId: {log_id}")
        self.fsm.feed_game_log_line(f"PrepareLevel '{map_name}'")

    # -------------------------------------------------------------------------
    # 1. Обычный килл / Мульти-килл / Обычная смерть
    # -------------------------------------------------------------------------
    def test_single_kill_and_multi_kill(self):
        """Проверка фиксации одиночного фрага (по таймауту 1.2с) и мульти-килла."""
        self._start_standard_match()
        self.assertTrue(self.fsm.session_active)
        self.assertEqual(len(self.session_starts), 1)

        # 1. Одиночный килл
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[kill_1]")
        self.assertEqual(self.fsm.kills, 0, "Килл должен быть в буфере подтверждения")
        self.assertEqual(len(self.fsm.pending_highlights), 1)

        # Продвигаем часы на 1.25с — таймаут подтверждения срабатывает
        self.clock.advance(1.25)
        self.assertEqual(self.fsm.kills, 1)
        self.assertEqual(self.events[-1][0], "kill")

        # 2. Мульти-килл (два хайлайта с интервалом 0.4с)
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[kill_2]")
        self.clock.advance(0.4)
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[kill_3]")
        self.assertEqual(self.fsm.kills, 1)

        # Продвигаем часы еще на 1.3с — оба должны подтвердиться
        self.clock.advance(1.3)
        self.assertEqual(self.fsm.kills, 3)
        self.assertEqual(self.fsm.deaths, 0)
        self.assertEqual(self.fsm.trades, 0)

    def test_normal_death_and_revive(self):
        """Проверка фиксации обычной смерти (NVIDIA + death/StateMachine) и оживления."""
        self._start_standard_match()

        # Хайлайт смерти приходит за 0.1с до экрана смерти в Game.log
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[death_highlight]")
        self.clock.advance(0.1)
        self.fsm.feed_game_log_line("Starting UI state machine 'death/StateMachine'...")

        self.assertEqual(self.fsm.deaths, 1)
        self.assertEqual(self.fsm.kills, 0)
        self.assertTrue(self.fsm.death_screen_open)
        self.assertTrue(self.fsm.death_screen_paired)
        self.assertEqual(self.events[-1][0], "death")

        # Напарник поднял (воскрешение)
        self.clock.advance(5.0)
        self.fsm.feed_game_log_line("Stopping UI state machine 'death/StateMachine'...")
        self.assertFalse(self.fsm.death_screen_open)
        self.assertFalse(self.fsm.death_screen_paired)
        self.assertEqual(self.events[-1][0], "revive")

    # -------------------------------------------------------------------------
    # 2. Размен (Trade kill) в окне 1.0 с
    # -------------------------------------------------------------------------
    def test_trade_kill_within_window(self):
        """Проверка фиксации взаимного размена (Trade) в окне 1.0 с."""
        self._start_standard_match()

        # Охотник погибает
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[my_death]")
        self.clock.advance(0.1)
        self.fsm.feed_game_log_line("Starting UI state machine 'death/StateMachine'...")
        self.assertEqual(self.fsm.deaths, 1)

        # Через 0.4с (в пределах TRADE_WINDOW = 1.0с) прилетает пуля охотника и убивает врага
        self.clock.advance(0.4)
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[enemy_death_trade]")

        self.assertEqual(self.fsm.kills, 1)
        self.assertEqual(self.fsm.deaths, 1)
        self.assertEqual(self.fsm.trades, 1)
        self.assertEqual(self.events[-1][0], "trade")

    # -------------------------------------------------------------------------
    # 3. Посмертный фраг ловушкой при открытом экране смерти
    # -------------------------------------------------------------------------
    def test_posthumous_trap_kill_while_dead(self):
        """Проверка посмертного килла капканом/ядом спустя 15с после спаренной гибели."""
        self._start_standard_match()

        # Гибель зафиксирована и спарена
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[death_1]")
        self.clock.advance(0.05)
        self.fsm.feed_game_log_line("Starting UI state machine 'death/StateMachine'...")
        self.assertEqual(self.fsm.deaths, 1)
        self.assertTrue(self.fsm.death_screen_paired)

        # Прошло 15 секунд (много больше TRADE_WINDOW 1.0с), охотник всё ещё лежит на экране смерти
        self.clock.advance(15.0)
        # Враг наступает на нашу ловушку — NVIDIA пишет хайлайт
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[trap_kill]")

        self.assertEqual(self.fsm.kills, 1)
        self.assertEqual(self.fsm.deaths, 1)
        self.assertEqual(self.fsm.trades, 0, "Посмертный фраг вне 1.0с не должен быть разменом")

    # -------------------------------------------------------------------------
    # 4. Бесшовный реконнект живым и мёртвым
    # -------------------------------------------------------------------------
    def test_seamless_reconnect_alive(self):
        """Бесшовный реконнект при аварийном вылете живым."""
        self._start_standard_match(ip="195.133.60.1:20000", log_id="LOG_RECON_1")
        # Совершаем килл
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[k1]")
        self.clock.advance(1.25)
        self.assertEqual(self.fsm.kills, 1)
        match_id_before = self.fsm.match_id

        # Аварийный вылет (Alt+F4 / crash)
        self.fsm.feed_game_log_line("Client_OnDisconnect: reason=ConnectionLost")
        self.assertFalse(self.fsm.session_active)
        self.assertTrue(self.fsm.awaiting_reconnect)
        self.assertEqual(self.events[-1][0], "reconnect_pause")

        # Перезапуск игры, коннект к тому же серверу
        self.clock.advance(60.0) # Прошла минута
        self.fsm.feed_game_log_line("Connection requested to: 195.133.60.1:20000")
        self.fsm.feed_game_log_line("starting mission with LogId: LOG_RECON_1")
        self.fsm.feed_game_log_line("PrepareLevel 'levels/colorado'")

        # Матч бесшовно продолжен
        self.assertTrue(self.fsm.session_active)
        self.assertFalse(self.fsm.awaiting_reconnect)
        self.assertEqual(self.fsm.match_id, match_id_before)
        self.assertEqual(self.fsm.kills, 1)
        self.assertEqual(self.events[-1][0], "reconnect")

    def test_seamless_reconnect_dead_suppression(self):
        """Бесшовный реконнект после вылета мёртвым: подавление повторной смерти."""
        self._start_standard_match(ip="195.133.60.2:20000", log_id="LOG_RECON_DEAD")

        # Охотник погибает
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[d1]")
        self.clock.advance(0.05)
        self.fsm.feed_game_log_line("Starting UI state machine 'death/StateMachine'...")
        self.assertEqual(self.fsm.deaths, 1)

        # Вылет на экране смерти
        self.fsm.feed_game_log_line("Client_OnDisconnect: reason=Crash")
        self.assertTrue(self.fsm.was_dead_before_dc)

        # Переподключение
        self.clock.advance(45.0)
        self.fsm.feed_game_log_line("Connection requested to: 195.133.60.2:20000")
        self.fsm.feed_game_log_line("starting mission with LogId: LOG_RECON_DEAD")
        self.fsm.feed_game_log_line("PrepareLevel 'levels/colorado'")

        # При загрузке CryEngine снова открывает death/StateMachine
        self.clock.advance(5.0)
        self.fsm.feed_game_log_line("Starting UI state machine 'death/StateMachine'...")
        self.assertEqual(self.fsm.deaths, 1, "Смерть не должна дублироваться при реконнекте!")

        # И NVIDIA шлет хайлайт загрузки на экран смерти
        self.clock.advance(0.5)
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[reload_death_hl]")
        self.assertEqual(self.fsm.deaths, 1, "Хайлайт перезагрузки экрана смерти должен быть подавлен!")

    # -------------------------------------------------------------------------
    # 5. Корректность расчёта длительности при вылете
    # -------------------------------------------------------------------------
    def test_reconnect_timeout_47m_duration_cap(self):
        """Истечение 47 минут ожидания реконнекта ограничивает длительность матча."""
        self._start_standard_match()
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[k1]")
        self.clock.advance(1.25)

        # Аварийный обрыв связи
        self.fsm.feed_game_log_line("Client_OnDisconnect: reason=ConnectionLost")
        self.assertTrue(self.fsm.awaiting_reconnect)

        # Прошло 47 минут (2820 секунд)
        self.clock.advance(47 * 60 + 5)

        # Сессия должна быть закрыта по таймауту
        self.assertFalse(self.fsm.session_active)
        self.assertFalse(self.fsm.awaiting_reconnect)
        self.assertEqual(len(self.session_ends), 1)
        end_stats = self.session_ends[0]
        self.assertEqual(end_stats["reason"], "reconnect_timeout_47m")
        self.assertLessEqual(end_stats["duration_seconds"], 47 * 60)

    # -------------------------------------------------------------------------
    # 6. Завершение матча _end_session с висящим хайлайтом в буфере
    # -------------------------------------------------------------------------
    def test_end_session_sweeps_pending_highlight(self):
        """Хайлайт в буфере подтверждения корректно начисляется при выходе в лобби."""
        self._start_standard_match()

        # Хайлайт получен прямо перед выходом (за 0.3с до меню)
        self.fsm.feed_nvidia_line("Posted Request HLSetVideo[last_second_kill]")
        self.clock.advance(0.3)

        # Выход в послематчевое лобби
        self.fsm.feed_game_log_line("Starting UI state machine 'lobby/StateMachine'...")

        self.assertFalse(self.fsm.session_active)
        self.assertEqual(len(self.session_ends), 1)
        self.assertEqual(self.session_ends[0]["kills"], 1, "Висящий хайлайт должен быть засчитан как килл!")

    # -------------------------------------------------------------------------
    # 7. Пропуск пустого матча 0K/0D
    # -------------------------------------------------------------------------
    def test_empty_match_stats(self):
        """Матч без событий фиксирует 0 киллов и 0 смертей для фильтрации."""
        self._start_standard_match()
        self.clock.advance(180.0)
        self.fsm.feed_game_log_line("Starting UI state machine 'lobby/StateMachine'...")

        self.assertEqual(len(self.session_ends), 1)
        stats = self.session_ends[0]
        self.assertEqual(stats["kills"], 0)
        self.assertEqual(stats["deaths"], 0)
        self.assertEqual(stats["duration_seconds"], 180)


if __name__ == "__main__":
    unittest.main()
