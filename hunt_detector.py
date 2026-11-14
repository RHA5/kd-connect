import os
import sys
import re

HUNT_FOLDER_NAMES = [
    "Hunt Showdown 1896",
    "Hunt Showdown"
]

def get_steam_registry_roots():
    roots = []
    if sys.platform != 'win32':
        return roots

    import winreg
    registry_keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")
    ]

    for hkey, subkey, val_name in registry_keys:
        try:
            with winreg.OpenKey(hkey, subkey) as key:
                val, _ = winreg.QueryValueEx(key, val_name)
                if val:
                    normalized = os.path.normpath(val)
                    if os.path.exists(normalized) and normalized not in roots:
                        roots.append(normalized)
        except Exception:
            pass

    return roots

def parse_library_folders(steam_path):
    libraries = []
    if not steam_path or not os.path.exists(steam_path):
        return libraries

    vdf_path = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    if not os.path.exists(vdf_path):
        vdf_path = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")

    if os.path.exists(vdf_path):
        try:
            with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            matches = re.findall(r'"path"\s+"([^"]+)"', content)
            for m in matches:
                clean_path = os.path.normpath(m.replace('\\\\', '\\'))
                if os.path.exists(clean_path) and clean_path not in libraries:
                    libraries.append(clean_path)
        except Exception:
            pass

    # Ensure main steam_path is in libraries
    norm_main = os.path.normpath(steam_path)
    if norm_main not in libraries and os.path.exists(norm_main):
        libraries.append(norm_main)

    return libraries

def get_fixed_drives():
    """
    Быстрый перебор дисков: возвращает список букв только локальных фиксированных дисков (DRIVE_FIXED=3, DRIVE_RAMDISK=6).
    Исключает сетевые диски (DRIVE_REMOTE=4), оптические приводы (DRIVE_CDROM=5) и съемные носители (DRIVE_REMOVABLE=2).
    """
    if sys.platform != 'win32':
        return ["C"]

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        bitmask = kernel32.GetLogicalDrives()
        fixed = []
        for i in range(26):
            if bitmask & (1 << i):
                drive_letter = chr(ord('A') + i)
                root_path = f"{drive_letter}:\\"
                # 3 = DRIVE_FIXED, 6 = DRIVE_RAMDISK
                if kernel32.GetDriveTypeW(root_path) in (3, 6):
                    fixed.append(drive_letter)
        return fixed or ["C"]
    except Exception:
        return ["C", "D", "E"]

def get_all_steam_libraries():
    libraries = []
    steam_roots = get_steam_registry_roots()

    # 1. Библиотеки из реестра Steam
    for root in steam_roots:
        parsed = parse_library_folders(root)
        for lib in parsed:
            if lib not in libraries:
                libraries.append(lib)

    # 2. Быстрый поиск только по известным путям на локальных фиксированных дисках
    known_subdirs = [
        "Steam",
        "SteamLibrary",
        "Program Files (x86)\\Steam",
        "Program Files\\Steam",
        "Games\\SteamLibrary",
        "Games\\Steam",
        "steam1"
    ]
    for drive in get_fixed_drives():
        for sub in known_subdirs:
            cand = f"{drive}:\\{sub}"
            norm_cand = os.path.normpath(cand)
            if os.path.exists(norm_cand) and norm_cand not in libraries:
                libraries.append(norm_cand)
                # Если в этой папке лежит libraryfolders.vdf, также парсим её
                for extra_lib in parse_library_folders(norm_cand):
                    if extra_lib not in libraries:
                        libraries.append(extra_lib)

    return libraries

def detect_hunt_directory(custom_path=None):
    """
    Returns tuple: (hunt_dir, game_log_path, status)
    status: 'ok', 'log_missing_yet', 'not_found'
    """
    if custom_path:
        norm_custom = os.path.normpath(custom_path)
        if os.path.exists(norm_custom):
            # 1. Прямая проверка внутри выбранной папки
            direct_log = os.path.join(norm_custom, "user", "Game.log")
            if os.path.exists(direct_log):
                return norm_custom, direct_log, 'ok'
            if os.path.exists(os.path.join(norm_custom, "hunt.exe")) or os.path.exists(os.path.join(norm_custom, "Hunt.exe")) or os.path.exists(os.path.join(norm_custom, "bin")):
                return norm_custom, direct_log, 'log_missing_yet'

            # 2. Проверка, если выбрана папка common или steamapps
            for folder_name in HUNT_FOLDER_NAMES:
                sub_dir = os.path.join(norm_custom, folder_name)
                if os.path.exists(sub_dir):
                    sub_log = os.path.join(sub_dir, "user", "Game.log")
                    if os.path.exists(sub_log):
                        return sub_dir, sub_log, 'ok'
                    return sub_dir, sub_log, 'log_missing_yet'

            # Пользователь выбрал папку, но в ней нет файлов Hunt Showdown
            return norm_custom, None, 'not_found'
        else:
            return norm_custom, None, 'not_found'

    # Автопоиск по всем библиотекам Steam
    found_games = []
    libraries = get_all_steam_libraries()
    for lib in libraries:
        for folder_name in HUNT_FOLDER_NAMES:
            hunt_dir = os.path.join(lib, "steamapps", "common", folder_name)
            if os.path.exists(hunt_dir):
                norm_dir = os.path.normpath(hunt_dir)
                game_log = os.path.join(norm_dir, "user", "Game.log")
                has_log = os.path.exists(game_log)
                mtime = os.path.getmtime(game_log) if has_log else 0
                is_1896 = 1 if "1896" in folder_name else 0
                has_exe = os.path.exists(os.path.join(norm_dir, "hunt.exe")) or os.path.exists(os.path.join(norm_dir, "Hunt.exe")) or os.path.exists(os.path.join(norm_dir, "bin"))
                
                if has_log:
                    found_games.append({
                        "dir": norm_dir,
                        "log": game_log,
                        "status": "ok",
                        "mtime": mtime,
                        "is_1896": is_1896
                    })
                elif has_exe:
                    found_games.append({
                        "dir": norm_dir,
                        "log": game_log,
                        "status": "log_missing_yet",
                        "mtime": 0,
                        "is_1896": is_1896
                    })

    if found_games:
        # 1. Если на диске присутствует хотя бы одна папка новой версии "1896",
        # работаем СТРОГО с ней, полностью игнорируя старые заброшенные папки без "1896"
        games_1896 = [g for g in found_games if g["is_1896"] == 1]
        target_pool = games_1896 if games_1896 else found_games

        # 2. Ищем папки с логами внутри выбранного целевого пула
        pool_with_log = [g for g in target_pool if g["status"] == "ok"]
        if len(pool_with_log) == 1:
            return pool_with_log[0]["dir"], pool_with_log[0]["log"], "ok"
        elif len(pool_with_log) > 1:
            pool_with_log.sort(key=lambda x: x["mtime"], reverse=True)
            return pool_with_log[0]["dir"], pool_with_log[0]["log"], "ok"

        # 3. Если логов в пуле еще нет (требуется первый запуск актуальной версии)
        pool_without_log = [g for g in target_pool if g["status"] == "log_missing_yet"]
        if pool_without_log:
            return pool_without_log[0]["dir"], pool_without_log[0]["log"], "log_missing_yet"

    return None, None, 'not_found'

def _is_any_process_running(target_names):
    """Проверяет, запущен ли хотя бы один из указанных процессов Windows."""
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        import ctypes.wintypes
        TH32CS_SNAPPROCESS = 0x00000002
        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ('dwSize', ctypes.wintypes.DWORD),
                ('cntUsage', ctypes.wintypes.DWORD),
                ('th32ProcessID', ctypes.wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.POINTER(ctypes.wintypes.ULONG)),
                ('th32ModuleID', ctypes.wintypes.DWORD),
                ('cntThreads', ctypes.wintypes.DWORD),
                ('th32ParentProcessID', ctypes.wintypes.DWORD),
                ('pcPriClassBase', ctypes.wintypes.LONG),
                ('dwFlags', ctypes.wintypes.DWORD),
                ('szExeFile', ctypes.c_char * 260)
            ]
        hSnapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if hSnapshot == -1:
            return False
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        found = False
        if ctypes.windll.kernel32.Process32First(hSnapshot, ctypes.byref(entry)):
            while True:
                exe = entry.szExeFile.decode('utf-8', errors='ignore').lower()
                if exe in target_names:
                    found = True
                    break
                if not ctypes.windll.kernel32.Process32Next(hSnapshot, ctypes.byref(entry)):
                    break
        ctypes.windll.kernel32.CloseHandle(hSnapshot)
        return found
    except Exception:
        return False

def is_nvidia_overlay_running():
    """Проверяет, активна ли служба внутриигрового оверлея NVIDIA (NVIDIA Share / nvsphelper64)."""
    return _is_any_process_running({'nvidiashare.exe', 'nvsphelper64.exe'})

def is_hunt_running():
    """Проверяет, запущен ли процесс Hunt: Showdown 1896 (HuntGame.exe)."""
    return _is_any_process_running({'huntgame.exe'})

def detect_nvidia_log():
    """
    Проверяет статус оверлея NVIDIA и находит последний HuntGame_*.log.
    Возвращает: (nvidia_log_path, status, message)
    status: 'ok', 'error'
    """
    # 1. Проверяем работу процесса службы оверлея
    if not is_nvidia_overlay_running():
        return None, 'error', 'Внутриигровой оверлей выключен или не установлен'

    # 2. Проверяем наличие папки GfeSDK
    local_app_data = os.environ.get('LOCALAPPDATA', '')
    if not local_app_data:
        return None, 'error', 'Системная папка AppData недоступна'

    gfesdk_dir = os.path.join(local_app_data, 'NVIDIA Corporation', 'GfeSDK')
    if not os.path.exists(gfesdk_dir):
        return None, 'error', 'Папка оверлея не найдена (требуется один запуск Hunt)'

    # 3. Ищем актуальный файл лога
    try:
        candidates = []
        for f in os.listdir(gfesdk_dir):
            if f.startswith('HuntGame') and f.endswith('.log'):
                full_path = os.path.join(gfesdk_dir, f)
                try:
                    mtime = os.path.getmtime(full_path)
                    candidates.append((mtime, full_path))
                except Exception:
                    pass

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1], 'ok', 'Оверлей активен'

        return None, 'ok', 'Оверлей активен'
    except Exception as e:
        return None, 'error', f'Ошибка доступа к папке оверлея: {e}'
