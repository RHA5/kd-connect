"""
KD Connect UI Simulator (Интерактивный симулятор состояний интерфейса)
Запуск: python tools/simulate_ui.py

Позволяет мгновенно переключать живое окно приложения во все состояния из Mindmap:
- Онбординг (Слайды 1-5)
- Мастер настройки (Шаги 1-3, ошибки оверлея и игры)
- Баннеры ожидания (Обновление, конфликты, ошибки)
- Состояние боя (Таймеры, счетчик)
- Все 4 модальных окна (Настроение, прерывание боя, обрыв связи, ошибка сохранения)
- Экран сверки (#section-verify) с проверкой типографики
- Экран настроек (v0.12.0 и кнопка полного сброса)
"""
import os
import sys
import threading
import time
import webview

# Mock API для безопасной симуляции (никаких обращений к БД, токенам и файлам)
class MockApi:
    def __init__(self):
        self.systems_state = {
            "nvidia": {"status": "ok", "message": "Оверлей активен"},
            "game": {"status": "ok", "message": "Логи найдены", "dir": "C:\\Games\\Hunt Showdown 1896"},
            "profile": {"status": "ok", "username": "Tester_Hunter", "base_kills": 150, "base_deaths": 100},
            "config": {}
        }

    def get_version(self):
        return "v0.12.1"

    def check_systems(self):
        return self.systems_state

    def check_active_session(self):
        return {"success": True, "active_session": None}

    def start_session(self, *args, **kwargs):
        return {"success": True, "session_id": "sim-session-id"}

    def end_session(self, *args, **kwargs):
        return {"success": True}

    def force_close_session_local(self):
        return {"success": True}

    def consume_session_lost(self):
        return False

    def close(self):
        if webview.windows:
            webview.windows[0].destroy()
        sys.exit(0)

    def start_window_drag(self):
        pass


api = MockApi()
window = None

def run_js(code):
    if window:
        try:
            window.evaluate_js(code)
        except Exception as e:
            print(f"[JS ERR] {e}")


def reset_all_screens():
    """Скрывает все модалки и экраны перед показом целевого состояния."""
    run_js("""
        ['section-disclaimer', 'section-wizard', 'section-idle', 'section-active', 'section-verify', 'settings-view'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.classList.add('hidden');
        });
        ['modal-mood', 'modal-confirm-combat', 'modal-confirm-pending', 'modal-verify-error'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.classList.add('hidden');
        });
        ['update-banner', 'idle-game-error-box', 'idle-nv-error-box', 'idle-manual-conflict-box', 'idle-delivery-box'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.classList.add('hidden');
        });
    """)


def action_slide(n):
    reset_all_screens()
    run_js(f"""
        document.getElementById('section-disclaimer').classList.remove('hidden');
        const closeTopBtn = document.getElementById('btn-disclaimer-close-top');
        if (closeTopBtn) closeTopBtn.classList.add('hidden');
        goToDisclaimerSlide({n});
    """)


def action_wizard_nv_fail():
    reset_all_screens()
    api.systems_state["nvidia"] = {"status": "error", "message": "Оверлей NVIDIA выключен"}
    run_js("""
        document.getElementById('section-wizard').classList.remove('hidden');
        goToWizardStep(1);
        applyStateToUi({
            nvidia: { status: 'error', message: 'Оверлей NVIDIA выключен' },
            game: { status: 'ok', dir: 'C:\\\\Games\\\\Hunt Showdown 1896' },
            profile: { status: 'ok', username: 'Tester_Hunter' },
            config: {}
        });
    """)


def action_wizard_nv_ok():
    reset_all_screens()
    api.systems_state["nvidia"] = {"status": "ok", "message": "Оверлей активен"}
    run_js("""
        document.getElementById('section-wizard').classList.remove('hidden');
        goToWizardStep(1);
        applyStateToUi({
            nvidia: { status: 'ok', message: 'Оверлей активен' },
            game: { status: 'ok', dir: 'C:\\\\Games\\\\Hunt Showdown 1896' },
            profile: { status: 'ok', username: 'Tester_Hunter' },
            config: {}
        });
    """)


def action_wizard_game_fail():
    reset_all_screens()
    api.systems_state["game"] = {"status": "not_found", "message": "Не удалось автоматически найти установленную игру в Steam"}
    run_js("""
        document.getElementById('section-wizard').classList.remove('hidden');
        goToWizardStep(2);
        applyStateToUi({
            nvidia: { status: 'ok', message: 'Оверлей активен' },
            game: { status: 'not_found', message: 'Не удалось автоматически найти установленную игру в Steam' },
            profile: { status: 'ok', username: 'Tester_Hunter' },
            config: {}
        });
    """)


def action_wizard_token():
    reset_all_screens()
    run_js("""
        document.getElementById('section-wizard').classList.remove('hidden');
        goToWizardStep(3);
    """)


def action_idle_normal():
    reset_all_screens()
    api.systems_state["profile"] = {"status": "ok", "username": "Tester_Hunter", "manual_conflict": False}
    api.systems_state["nvidia"] = {"status": "ok"}
    api.systems_state["game"] = {"status": "ok", "dir": "C:\\\\Games\\\\Hunt Showdown 1896"}
    run_js("""
        document.getElementById('section-idle').classList.remove('hidden');
        applyStateToUi({
            nvidia: { status: 'ok' },
            game: { status: 'ok', dir: 'C:\\\\Games\\\\Hunt Showdown 1896' },
            profile: { status: 'ok', username: 'Tester_Hunter' },
            config: {}
        });
        const btn = document.getElementById('btn-start-session');
        if (btn) { btn.disabled = false; btn.classList.remove('opacity-40', 'cursor-not-allowed'); }
    """)


def action_banner_update():
    reset_all_screens()
    run_js("""
        document.getElementById('section-idle').classList.remove('hidden');
        document.getElementById('idle-desc-box').classList.remove('hidden');
        const b = document.getElementById('update-banner');
        if (b) {
            b.classList.remove('hidden');
            const txt = document.getElementById('update-banner-text');
            if (txt) txt.innerText = 'Доступно обновление v0.12.1';
        }
    """)


def action_banner_conflict():
    reset_all_screens()
    api.systems_state["profile"] = {"status": "ok", "username": "Tester_Hunter", "manual_conflict": True}
    run_js("""
        document.getElementById('section-idle').classList.remove('hidden');
        applyStateToUi({
            nvidia: { status: 'ok' },
            game: { status: 'ok' },
            profile: { status: 'ok', username: 'Tester_Hunter', manual_conflict: true },
            config: {}
        });
    """)


def action_banner_nv_error():
    reset_all_screens()
    api.systems_state["nvidia"] = {"status": "error", "message": "Оверлей отключен"}
    run_js("""
        document.getElementById('section-idle').classList.remove('hidden');
        applyStateToUi({
            nvidia: { status: 'error', message: 'Оверлей отключен' },
            game: { status: 'ok' },
            profile: { status: 'ok', username: 'Tester_Hunter' },
            config: {}
        });
    """)


def action_banner_delivery_error():
    reset_all_screens()
    run_js("""
        document.getElementById('section-idle').classList.remove('hidden');
        document.getElementById('idle-desc-box').classList.remove('hidden');
        const b = document.getElementById('idle-delivery-box');
        if (b) b.classList.remove('hidden');
    """)


def action_in_combat():
    reset_all_screens()
    run_js("""
        document.getElementById('section-active').classList.remove('hidden');
        document.getElementById('app-status-dot').className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 pulse-glow-green';
        startSessionTimer(420);
        startMatchTimer(Math.floor(Date.now() / 1000) - 75);
        const cardTitle = document.getElementById('match-card-title');
        if (cardTitle) {
            cardTitle.innerText = 'В БОЮ';
            cardTitle.className = 'font-bold text-emerald-400 uppercase tracking-wider text-xs flex items-center gap-1.5';
        }
        document.getElementById('stat-session-kills').innerText = '2';
        document.getElementById('stat-session-deaths').innerText = '1';
        document.getElementById('stat-session-kd').innerText = '2.00';
        document.getElementById('match-kills-val').innerText = '2';
        document.getElementById('match-deaths-val').innerText = '0';
    """)


def action_modal_mood():
    action_idle_normal()
    run_js("document.getElementById('modal-mood').classList.remove('hidden');")


def action_modal_confirm_combat():
    action_in_combat()
    run_js("openConfirmCombatModal(2, 1, false);")


def action_modal_confirm_pending():
    action_in_combat()
    run_js("openConfirmPendingModal(2, 1, 3, false);")


def action_modal_verify_error():
    action_verify()
    run_js("openVerifyErrorModal();")


def action_verify():
    reset_all_screens()
    run_js("""
        document.getElementById('section-verify').classList.remove('hidden');
        appState = {
            profile: {
                active_session: {
                    session_id: 'sim-uuid',
                    session_matches_count: 4,
                    initial_kills: 120,
                    initial_deaths: 80,
                    session_kills: 3,
                    session_deaths: 1
                }
            }
        };
        currentSessionSnapshot = {
            sessionId: 'sim-uuid',
            matchesCount: 4,
            initialKills: 120,
            initialDeaths: 80,
            serverKills: 3,
            serverDeaths: 1,
            adjustedKills: 3,
            adjustedDeaths: 1
        };
        renderVerificationUi();
        toggleVerifyEditMode(true);
    """)


def action_settings():
    action_idle_normal()
    run_js("toggleSettings(true);")


MENU_ACTIONS = {
    "1": ("Слайд 1: Только NVIDIA", lambda: action_slide(1)),
    "2": ("Слайд 2: Только для Hunt", lambda: action_slide(2)),
    "3": ("Слайд 3: Учет абсолютно всех смертей", lambda: action_slide(3)),
    "4": ("Слайд 4: Размены", lambda: action_slide(4)),
    "5": ("Слайд 5: Подсчет ассистов невозможен", lambda: action_slide(5)),
    "6": ("Визард Шаг 1: Ошибка NVIDIA (двухэтапная инструкция)", action_wizard_nv_fail),
    "7": ("Визард Шаг 1: Успех NVIDIA", action_wizard_nv_ok),
    "8": ("Визард Шаг 2: Игра не найдена", action_wizard_game_fail),
    "9": ("Визард Шаг 3: Токен профиля", action_wizard_token),
    "10": ("Лобби: Ожидание сессии (штатный вид)", action_idle_normal),
    "11": ("Баннер: Доступно обновление", action_banner_update),
    "12": ("Баннер: На сайте открыта ручная сессия (конфликт)", action_banner_conflict),
    "13": ("Баннер: Оверлей NVIDIA выключен", action_banner_nv_error),
    "14": ("Баннер: В очереди есть неотправленные матчи", action_banner_delivery_error),
    "15": ("Режим боя: В матче (HUD, таймер катки, фраги)", action_in_combat),
    "16": ("Модалка: Опрос настроения (#modal-mood)", action_modal_mood),
    "17": ("Модалка: Завершить во время боя? (#modal-confirm-combat)", action_modal_confirm_combat),
    "18": ("Модалка: Обрыв связи (#modal-confirm-pending)", action_modal_confirm_pending),
    "19": ("Модалка: Ошибка сохранения сверки (#modal-verify-error)", action_modal_verify_error),
    "20": ("ЭКРАН СВЕРКИ: Проверка гротеска и кнопок + / - (#section-verify)", action_verify),
    "21": ("Окно настроек: v0.12.0 и кнопка полного сброса", action_settings),
}


def console_loop():
    time.sleep(1.0)
    print("\n" + "="*60)
    print("  KD CONNECT UI SIMULATOR (СИМУЛЯТОР СОСТОЯНИЙ ИНТЕРФЕЙСА)")
    print("="*60)
    for key in sorted(MENU_ACTIONS.keys(), key=lambda x: int(x)):
        title = MENU_ACTIONS[key][0]
        print(f" [{key:>2}] {title}")
    print(" [ q] Выход")
    print("="*60)

    # Стартуем сразу со сверки (чтобы пользователь мгновенно увидел типографику)
    action_verify()
    print(">> Окно открыто в режиме: [20] ЭКРАН СВЕРКИ (#section-verify)")

    while True:
        try:
            cmd = input("\nВведите номер состояния (1-21) или 'q': ").strip().lower()
            if cmd in ('q', 'exit', 'quit'):
                print("Завершение работы симулятора...")
                if window:
                    window.destroy()
                break
            if cmd in MENU_ACTIONS:
                title, fn = MENU_ACTIONS[cmd]
                print(f">> Применяю: [{cmd}] {title}")
                fn()
            else:
                print("Неизвестная команда. Введите число от 1 до 21.")
        except (EOFError, KeyboardInterrupt):
            break


def main():
    global window
    kd_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    html_path = os.path.join(kd_root, "preview.html")

    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    # Запускаем консольный поток
    t = threading.Thread(target=console_loop, daemon=True)
    t.start()

    window = webview.create_window(
        'KD Connect - UI Simulator (v0.12.1)',
        html=html_content,
        js_api=api,
        width=430,
        height=660,
        resizable=True,
        frameless=False,  # Обычная рамка для удобного перемещения по экрану
        background_color='#09090b',
    )
    webview.start()


if __name__ == "__main__":
    main()
