#!/usr/bin/env python3
"""
iGame 硬件控制 HTTP API 服务
=============================
架构定位: 唯一的硬件交互层，对外提供轻量 RESTful HTTP API。
后端（计划 Rust）将通过 HTTP 请求调用这些 API，不再直接接触 DLL。
前端 Vue 与后端通信，对硬件层无感知。

依赖: Flask, pythonnet (clr), psutil (可选)
运行: python app.py  → 监听 0.0.0.0:5000
生产: waitress-serve --host=0.0.0.0 --port=5000 app:app
"""

import sys
import os
import json
import logging
from datetime import datetime

# ============================================================
# 路径设置: 将父目录加入 sys.path，以便引用 notebook_model / sensor_reader / bin/
# 同时切换工作目录到项目根目录，确保 DLL 查找路径正确（libusb-1.0_x64.dll 等）
# ============================================================
_PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

# 切换工作目录到项目根目录，iGameAPI DLL 依赖相对路径查找 libusb 等
try:
    os.chdir(_PARENT_DIR)
except Exception:
    pass

# 确保 iGameAPI DLL 目录在 PATH 中，让 libusb-1.0_x64.dll 等依赖能被找到
import ctypes
for _sub in ["", "bin", "bin/iGameAPI", "iGameAPI", "iGameAPI/N15_25", "bin/iGameAPI/N15_25"]:
    _dir = os.path.join(_PARENT_DIR, _sub) if _sub else _PARENT_DIR
    if os.path.isdir(_dir) and _dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _dir + ";" + os.environ.get("PATH", "")

# ============================================================
# 日志配置
# ============================================================
def _setup_logging():
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"hardware_api_{datetime.now().strftime('%Y%m%d')}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

_setup_logging()
logger = logging.getLogger("hardware_api")

# ============================================================
# 资源路径辅助函数（bin/ 等位于父目录）
# ============================================================
def get_parent_path(relative_path):
    """获取父目录下的资源文件路径，兼容 PyInstaller 打包"""
    if getattr(sys, "frozen", False):
        # 打包后：DLL 与 exe 在同一目录
        base = os.path.dirname(sys.executable)
    else:
        # 开发时：DLL 在项目根目录的 bin/ 下
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)

def get_bin_path(filename):
    """获取 bin/ 目录下的 DLL 文件路径"""
    if getattr(sys, "frozen", False):
        # 打包后：DLL 与 exe 在同一目录，不需要 bin/ 前缀
        return os.path.join(os.path.dirname(sys.executable), filename)
    else:
        # 开发时：DLL 在项目根目录的 bin/ 下
        return get_parent_path(os.path.join("bin", filename))

# ============================================================
# DLL 加载 (Central.iGame + iGameAPI.Notebook)
# ============================================================
import clr

_dll_error = None
_wmi = None
_win32 = None
_mcu = None
_model_mgr = None
_win_lock_ctrl = None
_EnumWinLock = None
_lhm_reader = None

def _load_central_dll():
    """加载 Central.iGame.dll，获取 Wmi/Win32/MCUControl"""
    global _wmi, _win32, _mcu, _dll_error
    dll_path = get_bin_path("Central.iGame.dll")
    if not os.path.exists(dll_path):
        _dll_error = f"Central.iGame.dll 不存在: {dll_path}"
        logger.error(_dll_error)
        return False
    try:
        clr.AddReference(dll_path)
        from Central import Wmi, Win32
        from Central.MCU import MCUControl
        _wmi = Wmi
        _win32 = Win32
        _mcu = MCUControl
        logger.info("Central.iGame.dll 加载成功")
        return True
    except Exception as e:
        _dll_error = f"加载 Central.iGame.dll 失败: {e}"
        logger.error(_dll_error)
        return False

def _load_notebook_dlls():
    """加载 iGameAPI.Notebook DLLs 及机型检测模块"""
    global _win_lock_ctrl, _EnumWinLock, _model_mgr
    try:
        contracts_dll = get_bin_path("iGameAPI.Notebook.Contracts.dll")
        common_dll = get_bin_path("iGameAPI.Notebook.Common.dll")
        clr.AddReference(contracts_dll)
        clr.AddReference(common_dll)
        from iGameAPI.Notebook.Contracts import IWinLockControl, EnumWinLock
        from iGameAPI.Notebook.Common import NotebookAPIFactory
        _EnumWinLock = EnumWinLock
        try:
            _notebook_api = NotebookAPIFactory.GetNotebookAPI()
            if _notebook_api is not None:
                if isinstance(_notebook_api, tuple):
                    for item in _notebook_api:
                        if hasattr(item, 'SetWinLock'):
                            _win_lock_ctrl = item
                            break
                    if _win_lock_ctrl is None:
                        _win_lock_ctrl = _notebook_api[0] if _notebook_api else None
                else:
                    _win_lock_ctrl = _notebook_api
            logger.info("IWinLockControl 可用" if _win_lock_ctrl else "IWinLockControl 初始化失败")
        except Exception as e:
            logger.warning(f"IWinLockControl 初始化失败: {e}")
    except Exception as e:
        logger.warning(f"加载 iGameAPI.Notebook DLL 失败: {e}")

    # 加载机型检测模块（来自父目录的 notebook_model.py）
    try:
        import notebook_model
        _model_mgr = notebook_model.get_model_manager()
        logger.info("机型检测模块加载成功")
    except Exception as e:
        logger.warning(f"机型检测模块加载失败: {e}")

def _load_lhm():
    """加载 LibreHardwareMonitor 传感器读取器"""
    global _lhm_reader
    try:
        from sensor_reader import LibreHardwareMonitorReader, SensorType
        _lhm_reader = LibreHardwareMonitorReader(auto_start=True)
        if _lhm_reader.open():
            logger.info("LibreHardwareMonitor 传感器读取器就绪")
        else:
            logger.warning(f"LibreHardwareMonitor 不可用: {_lhm_reader.error}")
            _lhm_reader = None
    except Exception as e:
        logger.warning(f"LibreHardwareMonitor 加载失败: {e}")
        _lhm_reader = None

# 执行 DLL 加载
_dll_loaded = _load_central_dll()
if _dll_loaded:
    _load_notebook_dlls()
    _load_lhm()

# ============================================================
# 硬件控制核心 — 运行时状态
# ============================================================

class HardwareController:
    """硬件控制器的简单状态包装

    不持久化配置（纯 API 层），仅维护运行时状态。
    """
    def __init__(self):
        self.same_speed = False
        self.win_lock = False
        self.is_custom_mode = False
        self.is_full_mode = False
        self.current_fan_mode = "auto"
        self.current_perf_mode = "未知"
        self.speed_conversion = 53
        self._cur_cpu_pct = 0
        self._cur_gpu_pct = 0
        self._init_mode_maps()

    def _get_active_sdk(self):
        """获取当前激活的 SDK 类型"""
        try:
            if _model_mgr:
                return _model_mgr.get_selected_sdk()
        except Exception:
            pass
        return 4  # 默认 SDK_N15_25

    def _init_mode_maps(self):
        """初始化性能/GPU 模式映射（从 notebook_model 获取 SDK 特定映射）"""
        sdk = self._get_active_sdk()
        try:
            from notebook_model import SDK_PERF_MODES, SDK_GPU_MODES, SDK_N15_25
            perf = SDK_PERF_MODES.get(sdk, SDK_PERF_MODES.get(SDK_N15_25, {}))
            gpu = SDK_GPU_MODES.get(sdk, SDK_GPU_MODES.get(SDK_N15_25, {}))
            self.perf_mode_map = {v: k for k, v in perf.items()}
            self.gpu_mode_map = {v: k for k, v in gpu.items()}
        except ImportError:
            self.perf_mode_map = {2: "狂暴模式", 1: "静音游戏", 0: "超长续航"}
            self.gpu_mode_map = {3: "集显模式", 1: "独显直连", 0: "混合模式"}
        if not self.perf_mode_map:
            self.perf_mode_map = {2: "狂暴模式", 1: "静音游戏", 0: "超长续航"}
        if not self.gpu_mode_map:
            self.gpu_mode_map = {3: "集显模式", 1: "独显直连", 0: "混合模式"}

_ctl = HardwareController()

# ============================================================
# 统一 DLL 调用封装
# ============================================================
class DllError(Exception):
    """DLL 调用统一异常"""
    pass

def call_dll(func, *args, error_msg="DLL调用失败"):
    """统一调用 DLL 函数并处理异常

    参数:
        func: DLL 函数引用（如 _wmi.GetCPUTem）
        *args: 传递给函数的参数
        error_msg: 自定义错误信息前缀

    返回:
        函数返回值

    抛出:
        DllError: DLL 加载失败或调用失败时抛出
    """
    if _dll_error:
        raise DllError(f"DLL未加载: {_dll_error}")
    try:
        return func(*args)
    except Exception as e:
        raise DllError(f"{error_msg}: {e}")

# ============================================================
# 硬件操作函数
# ============================================================

# ── 温度 ──
def get_cpu_temperature():
    """获取 CPU 温度（℃）"""
    val = call_dll(_wmi.GetCPUTem, error_msg="获取CPU温度失败")
    return round(float(val), 1)

def get_gpu_temperature():
    """获取 GPU 温度（℃）"""
    val = call_dll(_wmi.GetGPUTem, error_msg="获取GPU温度失败")
    return round(float(val), 1)

# ── 风扇转速 ──
def get_cpu_fan_speed():
    """获取 CPU 风扇转速（RPM）"""
    return call_dll(_wmi.GetCpufanSpeed, error_msg="获取CPU风扇转速失败")

def get_gpu_fan_speed():
    """获取 GPU 风扇转速（RPM）"""
    return call_dll(_wmi.GetGpufanSpeed, error_msg="获取GPU风扇转速失败")

# ── 风扇控制 ──
def set_fan_speed(cpu_speed, gpu_speed):
    """设置 CPU/GPU 风扇转速（绝对 RPM 值）"""
    if _ctl.same_speed:
        s = max(cpu_speed, gpu_speed)
        call_dll(_wmi.SetFanSpeed, s, s, error_msg="设置同速风扇转速失败")
    else:
        call_dll(_wmi.SetFanSpeed, cpu_speed, gpu_speed, error_msg="设置风扇转速失败")
    _ctl._cur_cpu_pct = round(cpu_speed / _ctl.speed_conversion) if _ctl.speed_conversion else 0
    _ctl._cur_gpu_pct = round(gpu_speed / _ctl.speed_conversion) if _ctl.speed_conversion else 0

def set_fan_control_open(enabled):
    """打开/关闭风扇手动控制 （enabled=True 进入手动模式）"""
    call_dll(_wmi.FanControlOpen, enabled, error_msg="FanControlOpen失败")
    _ctl.is_custom_mode = enabled
    _ctl.current_fan_mode = "manual" if enabled else "auto"

def set_fan_full_mode(enabled):
    """设置/取消全速模式"""
    call_dll(_wmi.SetFanFullMode, enabled, error_msg="SetFanFullMode失败")
    _ctl.is_full_mode = enabled
    if enabled:
        _ctl.is_custom_mode = False

def get_fan_full_mode():
    """查询全速模式状态"""
    return call_dll(_wmi.GetFanFullMode, error_msg="GetFanFullMode失败") != 0

# ── 性能模式 ──
def set_performance_mode(mode_name):
    """设置性能模式（如 "狂暴模式"、"静音游戏"、"超长续航"）"""
    sdk = _ctl._get_active_sdk()
    try:
        from notebook_model import SDK_PERF_MODES, SDK_N15_25
        modes = SDK_PERF_MODES.get(sdk, SDK_PERF_MODES.get(SDK_N15_25, {}))
    except ImportError:
        modes = {"狂暴模式": 2, "静音游戏": 1, "超长续航": 0}
    code = modes.get(mode_name)
    if code is None:
        raise DllError(f"未知的性能模式: {mode_name}")
    call_dll(_wmi.SetPerformanceMode, code, error_msg=f"设置{mode_name}失败")
    _ctl.current_perf_mode = mode_name
    logger.info(f"性能模式: {mode_name} (code={code})")

def get_performance_mode():
    """查询当前性能模式 → {"mode": str, "code": int}"""
    code = call_dll(_wmi.GetPerformanceMode, error_msg="获取性能模式失败")
    name = _ctl.perf_mode_map.get(code, f"未知({code})")
    return {"mode": name, "code": code}

def get_available_performance_modes():
    """可用的性能模式列表"""
    return list(_ctl.perf_mode_map.values())

# ── GPU 模式 ──
def set_gpu_mode(mode_name):
    """设置 GPU 模式（"混合模式"/"独显直连"/"集显模式"）"""
    sdk = _ctl._get_active_sdk()
    try:
        from notebook_model import SDK_GPU_MODES, SDK_N15_25
        modes = SDK_GPU_MODES.get(sdk, SDK_GPU_MODES.get(SDK_N15_25, {}))
    except ImportError:
        modes = {"混合模式": 0, "独显直连": 1, "集显模式": 3}
    code = modes.get(mode_name)
    if code is None:
        raise DllError(f"未知的GPU模式: {mode_name}")
    call_dll(_wmi.SetGPUMode, code, error_msg=f"设置{mode_name}失败")
    logger.info(f"GPU模式: {mode_name} (code={code})")

def get_gpu_mode():
    """查询当前 GPU 模式 → {"mode": str, "code": int}"""
    code = call_dll(_wmi.GetGPUMode, error_msg="获取GPU模式失败")
    name = _ctl.gpu_mode_map.get(code, f"未知({code})")
    return {"mode": name, "code": code}

def get_available_gpu_modes():
    """可用的 GPU 模式列表"""
    return list(_ctl.gpu_mode_map.values())

# ── 充电模式 ──
def get_charge_method():
    """获取充电控制方式: 'wmi' | 'insyde' | 'emdacpi'"""
    try:
        if _model_mgr:
            return _model_mgr.get_model_capabilities().get("charge_method", "wmi")
    except Exception:
        pass
    return "wmi"

def set_charge_mode(mode_name, start=40, stop=80):
    """设置充电模式

    mode_name: "最大电池电量" | "推荐电池充电" | "自定义充电"
    start/stop: 自定义模式下的电量百分比
    """
    cm = get_charge_method()

    if mode_name == "最大电池电量":
        # 禁用电量保护，充到 100%
        if cm == "insyde":
            try:
                call_dll(_wmi.SetFlexiChargerSettings, False, 95, 100, error_msg="Clevo充电失败")
            except DllError:
                call_dll(_wmi.ChargingOptimize, False, error_msg="ChargingOptimize失败")
        elif cm == "emdacpi":
            try:
                call_dll(_wmi.DisableFlexiCharge, error_msg="DisableFlexiCharge失败")
            except DllError:
                call_dll(_wmi.ChargingOptimize, False, error_msg="ChargingOptimize失败")
        else:
            call_dll(_wmi.ChargingOptimize, False, error_msg="ChargingOptimize失败")

    elif mode_name == "推荐电池充电":
        if cm == "insyde":
            try:
                call_dll(_wmi.SetFlexiChargerSettings, True, 70, 80, error_msg="Clevo充电失败")
            except DllError:
                call_dll(_wmi.ChargingOptimize, True, error_msg="ChargingOptimize失败")
                call_dll(_wmi.SetBatteryMin, 70, error_msg="SetBatteryMin失败")
                call_dll(_wmi.SetBatteryMax, 80, error_msg="SetBatteryMax失败")
        elif cm == "emdacpi":
            try:
                call_dll(_wmi.SetInitCharge, 7, error_msg="SetInitCharge失败")
                call_dll(_wmi.SetStopCharge, 8, error_msg="SetStopCharge失败")
            except DllError:
                call_dll(_wmi.ChargingOptimize, True, error_msg="ChargingOptimize失败")
                call_dll(_wmi.SetBatteryMin, 70, error_msg="SetBatteryMin失败")
                call_dll(_wmi.SetBatteryMax, 80, error_msg="SetBatteryMax失败")
        else:
            call_dll(_wmi.ChargingOptimize, True, error_msg="ChargingOptimize失败")
            call_dll(_wmi.SetBatteryMin, 70, error_msg="SetBatteryMin失败")
            call_dll(_wmi.SetBatteryMax, 80, error_msg="SetBatteryMax失败")

    elif mode_name == "自定义充电":
        s, e = int(start), int(stop)
        if s > e: e = 100
        if cm == "insyde":
            try:
                call_dll(_wmi.SetFlexiChargerSettings, True, s, e, error_msg="Clevo充电失败")
            except DllError:
                call_dll(_wmi.ChargingOptimize, True, error_msg="ChargingOptimize失败")
                call_dll(_wmi.SetBatteryMin, s, error_msg="SetBatteryMin失败")
                call_dll(_wmi.SetBatteryMax, e, error_msg="SetBatteryMax失败")
        elif cm == "emdacpi":
            try:
                s_d = max(4, min(10, round(s / 10.0)))
                e_d = max(4, min(10, round(e / 10.0)))
                call_dll(_wmi.SetInitCharge, int(s_d), error_msg="SetInitCharge失败")
                call_dll(_wmi.SetStopCharge, int(e_d), error_msg="SetStopCharge失败")
            except DllError:
                call_dll(_wmi.ChargingOptimize, True, error_msg="ChargingOptimize失败")
                call_dll(_wmi.SetBatteryMin, s, error_msg="SetBatteryMin失败")
                call_dll(_wmi.SetBatteryMax, e, error_msg="SetBatteryMax失败")
        else:
            call_dll(_wmi.ChargingOptimize, True, error_msg="ChargingOptimize失败")
            call_dll(_wmi.SetBatteryMin, s, error_msg="SetBatteryMin失败")
            call_dll(_wmi.SetBatteryMax, e, error_msg="SetBatteryMax失败")
    else:
        raise DllError(f"未知的充电模式: {mode_name}")

    logger.info(f"充电模式: {mode_name} (start={start}, stop={stop})")

def get_charge_status():
    """查询充电状态 → {"mode", "start", "stop", "battery_level", "ac_power"}"""
    mode = "最大电池电量"
    start, stop = 40, 80
    cm = get_charge_method()

    try:
        if cm == "emdacpi":
            try:
                start = int(call_dll(_wmi.GetInitCharge, error_msg="GetInitCharge失败")) * 10
                stop = int(call_dll(_wmi.GetStopCharge, error_msg="GetStopCharge失败")) * 10
                mode = "自定义充电" if start != 70 or stop != 80 else "推荐电池充电"
            except DllError:
                pass
        else:
            try:
                result = call_dll(_wmi.GetBatteryChargeMode, error_msg="GetBatteryChargeMode失败")
                if isinstance(result, (tuple, list)) and len(result) >= 3:
                    mi = int(result[0])
                    start = int(result[1]) if result[1] else 40
                    stop = int(result[2]) if result[2] else 80
                    mode = {2: "最大电池电量", 1: "推荐电池充电", 4: "自定义充电"}.get(mi, "最大电池电量")
            except DllError:
                pass
    except Exception:
        pass

    battery_level = 0
    ac_power = False
    try:
        import psutil
        b = psutil.sensors_battery()
        if b:
            battery_level = b.percent
            ac_power = b.power_plugged
    except Exception:
        pass

    return {"mode": mode, "start": start, "stop": stop,
            "battery_level": battery_level, "ac_power": ac_power}

# ── 灯光控制 ──
BRIGHTNESS_MAP = {"亮度0": 0, "亮度1": 63, "亮度2": 85, "亮度3": 127, "亮度4": 255}
LIGHT_MODE_MAP = {"关闭": 0, "打开": 1, "常亮": 2, "呼吸": 3, "渐变": 4}

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def set_keyboard_light(mode, color="#ff0000", brightness="亮度3"):
    """设置键盘灯光。region=0。"""
    r, g, b = _hex_to_rgb(color)
    v = BRIGHTNESS_MAP.get(brightness, 127)
    if mode == "关闭":
        call_dll(_mcu.LightSwitch, 0, 0, r, g, b, v, error_msg="键盘灯关闭失败")
    else:
        call_dll(_mcu.LightSwitch, 0, 1, r, g, b, v, error_msg="键盘灯打开失败")
        cm = LIGHT_MODE_MAP.get(mode, 2)
        call_dll(_mcu.LightSwitch, 0, cm, r, g, b, v, error_msg="键盘灯模式失败")
    logger.info(f"键盘灯: {mode}, {color}, {brightness}")

def set_led_light(mode, color="#00ff00", brightness="亮度3"):
    """设置机身 LED。region=1。"""
    r, g, b = _hex_to_rgb(color)
    v = BRIGHTNESS_MAP.get(brightness, 127)
    if mode == "关闭":
        call_dll(_mcu.LightSwitch, 1, 0, r, g, b, v, error_msg="LED灯关闭失败")
    else:
        call_dll(_mcu.LightSwitch, 1, 1, r, g, b, v, error_msg="LED灯打开失败")
        cm = LIGHT_MODE_MAP.get(mode, 2)
        call_dll(_mcu.LightSwitch, 1, cm, r, g, b, v, error_msg="LED灯模式失败")
    logger.info(f"LED灯: {mode}, {color}, {brightness}")

def set_auto_light(enabled):
    """自动熄灯开关"""
    call_dll(_mcu.AutoCloselight, enabled, error_msg="AutoCloselight失败")
    logger.info(f"自动熄灯: {'ON' if enabled else 'OFF'}")

# ── Win 键锁 ──
def set_win_lock(enabled):
    global _win_lock_ctrl, _EnumWinLock
    if _win_lock_ctrl is not None and _EnumWinLock is not None:
        try:
            if isinstance(_win_lock_ctrl, tuple):
                for item in _win_lock_ctrl:
                    if hasattr(item, 'SetWinLock'):
                        flag = _EnumWinLock.Off if enabled else _EnumWinLock.On
                        call_dll(item.SetWinLock, flag, error_msg="SetWinLock(官方)失败")
                        _ctl.win_lock = enabled
                        return
            else:
                flag = _EnumWinLock.Off if enabled else _EnumWinLock.On
                call_dll(_win_lock_ctrl.SetWinLock, flag, error_msg="SetWinLock(官方)失败")
                _ctl.win_lock = enabled
                return
        except DllError:
            logger.warning("WinLock 官方接口失败，回退 Win32")
    try:
        call_dll(_win32.SetWinkeyLock, enabled, error_msg="SetWinkeyLock失败")
    except DllError:
        logger.warning("WinLock Win32 接口也失败，仅记录状态")
    _ctl.win_lock = enabled

def get_win_lock():
    return _ctl.win_lock

# ── LibreHardwareMonitor 传感器 ──
def get_lhm_sensors(sensor_type=None):
    """获取 LHM 传感器列表，可选按类型过滤"""
    if not _lhm_reader:
        return []
    try:
        _lhm_reader.refresh()
        result = []
        for entry in _lhm_reader.list_all():
            if entry.get("value") is None:
                continue
            if sensor_type and entry.get("type", "").lower() != sensor_type.lower():
                continue
            result.append({
                "name": entry["name"],
                "hw": entry.get("hw", "LHM"),
                "type": entry.get("type", "unknown"),
                "value": round(float(entry["value"]), 1),
            })
        return result
    except Exception as e:
        logger.error(f"LHM传感器读取失败: {e}")
        return []

def get_lhm_temperatures():
    """LHM 温度传感器子集"""
    return get_lhm_sensors(sensor_type="temperature")

# ── 机型信息 ──
def get_notebook_info():
    try:
        if _model_mgr:
            return _model_mgr.get_detection_info()
    except Exception as e:
        logger.error(f"机型信息获取失败: {e}")
    return {"model": "未知", "sdk": "未知"}

def get_model_capabilities():
    try:
        if _model_mgr:
            return _model_mgr.get_model_capabilities()
    except Exception as e:
        logger.error(f"机型能力获取失败: {e}")
    return {}

# ============================================================
# 初始化硬件到安全状态
# ============================================================
if _dll_loaded:
    for _action, _fn in [
        ("SetFanFullMode(False)", lambda: _wmi.SetFanFullMode(False)),
        ("FanControlOpen(False)", lambda: _wmi.FanControlOpen(False)),
    ]:
        try:
            _fn()
            logger.info(f"{_action} OK")
        except Exception as e:
            logger.error(f"初始化 {_action} 失败: {e}")
    try:
        _ctl.is_full_mode = _wmi.GetFanFullMode() != 0
    except Exception:
        pass
    try:
        mc = _wmi.GetPerformanceMode()
        _ctl.current_perf_mode = _ctl.perf_mode_map.get(mc, "未知")
    except Exception:
        pass

# ============================================================
# Flask HTTP API + 静态文件服务
# ============================================================
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

import flask.logging
flask.logging.default_handler.setLevel(logging.WARNING)

# Vue 前端静态文件目录
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")

# ── 静态文件服务（Vue 前端）──
@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/<path:path>")
def serve_static(path):
    file_path = os.path.join(FRONTEND_DIR, path)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return send_from_directory(FRONTEND_DIR, path)
    return send_from_directory(FRONTEND_DIR, "index.html")

# ── 健康检查 ──
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "dll_loaded": _dll_loaded, "dll_error": _dll_error})


# ── 温度 ──
@app.route("/api/hardware/cpu_temperature", methods=["GET"])
def api_cpu_temperature():
    """→ {"temperature": float, "unit": "Celsius"}"""
    try:
        return jsonify({"temperature": get_cpu_temperature(), "unit": "Celsius"})
    except DllError as e:
        return jsonify({"error": str(e), "temperature": 0, "unit": "Celsius"}), 500

@app.route("/api/hardware/gpu_temperature", methods=["GET"])
def api_gpu_temperature():
    """→ {"temperature": float, "unit": "Celsius"}"""
    try:
        return jsonify({"temperature": get_gpu_temperature(), "unit": "Celsius"})
    except DllError as e:
        return jsonify({"error": str(e), "temperature": 0, "unit": "Celsius"}), 500

@app.route("/api/hardware/temperatures", methods=["GET"])
def api_temperatures():
    """→ {"cpu": {...}, "gpu": {...}}"""
    try:
        return jsonify({
            "cpu": {"temperature": get_cpu_temperature(), "unit": "Celsius"},
            "gpu": {"temperature": get_gpu_temperature(), "unit": "Celsius"},
        })
    except DllError as e:
        return jsonify({"error": str(e)}), 500


# ── 风扇转速 ──
@app.route("/api/hardware/cpu_fan_speed", methods=["GET"])
def api_cpu_fan_speed():
    """→ {"fan_speed_rpm": int}"""
    try:
        return jsonify({"fan_speed_rpm": get_cpu_fan_speed()})
    except DllError as e:
        return jsonify({"error": str(e), "fan_speed_rpm": 0}), 500

@app.route("/api/hardware/gpu_fan_speed", methods=["GET"])
def api_gpu_fan_speed():
    """→ {"fan_speed_rpm": int}"""
    try:
        return jsonify({"fan_speed_rpm": get_gpu_fan_speed()})
    except DllError as e:
        return jsonify({"error": str(e), "fan_speed_rpm": 0}), 500

@app.route("/api/hardware/fan_speeds", methods=["GET"])
def api_fan_speeds():
    """→ {"cpu": {"fan_speed_rpm": ...}, "gpu": {...}}"""
    try:
        return jsonify({
            "cpu": {"fan_speed_rpm": get_cpu_fan_speed()},
            "gpu": {"fan_speed_rpm": get_gpu_fan_speed()},
        })
    except DllError as e:
        return jsonify({"error": str(e)}), 500


# ── 风扇控制 ──
@app.route("/api/fan/speed", methods=["POST"])
def api_set_fan_speed():
    """POST {"cpu_speed": int, "gpu_speed": int}"""
    try:
        d = request.get_json(force=True)
        cs, gs = int(d.get("cpu_speed", 0)), int(d.get("gpu_speed", 0))
        set_fan_speed(cs, gs)
        return jsonify({"ok": True, "cpu_speed": cs, "gpu_speed": gs})
    except DllError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/fan/custom_control", methods=["POST"])
def api_fan_custom_control():
    """POST {"enabled": bool} — 风扇手动/自动控制"""
    try:
        d = request.get_json(force=True)
        en = bool(d.get("enabled", False))
        set_fan_control_open(en)
        return jsonify({"ok": True, "custom_control": en, "fan_mode": _ctl.current_fan_mode})
    except DllError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/fan/full_mode", methods=["POST"])
def api_fan_full_mode():
    """POST {"enabled": bool} — 全速/强冷模式"""
    try:
        d = request.get_json(force=True)
        en = bool(d.get("enabled", False))
        set_fan_full_mode(en)
        return jsonify({"ok": True, "full_mode": en})
    except DllError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/fan/status", methods=["GET"])
def api_fan_status():
    try:
        is_full = get_fan_full_mode()
    except DllError:
        is_full = _ctl.is_full_mode
    return jsonify({
        "fan_mode": _ctl.current_fan_mode,
        "is_custom_mode": _ctl.is_custom_mode,
        "is_full_mode": is_full,
        "same_speed": _ctl.same_speed,
    })

@app.route("/api/fan/same_speed", methods=["POST"])
def api_fan_same_speed():
    """POST {"enabled": bool} — CPU/GPU同速"""
    d = request.get_json(force=True)
    _ctl.same_speed = bool(d.get("enabled", False))
    return jsonify({"ok": True, "same_speed": _ctl.same_speed})


# ── 性能模式 ──
@app.route("/api/perf/mode", methods=["GET"])
def api_get_perf_mode():
    try:
        return jsonify(get_performance_mode())
    except DllError as e:
        return jsonify({"error": str(e), "mode": _ctl.current_perf_mode}), 500

@app.route("/api/perf/mode", methods=["POST"])
def api_set_perf_mode():
    """POST {"mode": "狂暴模式"}"""
    d = request.get_json(force=True)
    mode = d.get("mode", "")
    if not mode:
        return jsonify({"ok": False, "error": "缺少 mode 参数"}), 400
    try:
        set_performance_mode(mode)
        return jsonify({"ok": True, "mode": mode})
    except DllError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/perf/modes", methods=["GET"])
def api_get_perf_modes():
    return jsonify({"modes": get_available_performance_modes(), "current": _ctl.current_perf_mode})


# ── GPU 模式 ──
@app.route("/api/gpu/mode", methods=["GET"])
def api_get_gpu_mode():
    try:
        return jsonify(get_gpu_mode())
    except DllError as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/gpu/mode", methods=["POST"])
def api_set_gpu_mode():
    """POST {"mode": "独显直连"}"""
    d = request.get_json(force=True)
    mode = d.get("mode", "")
    if not mode:
        return jsonify({"ok": False, "error": "缺少 mode 参数"}), 400
    try:
        set_gpu_mode(mode)
        return jsonify({"ok": True, "mode": mode})
    except DllError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/gpu/modes", methods=["GET"])
def api_get_gpu_modes():
    return jsonify({"modes": get_available_gpu_modes()})


# ── 充电 ──
@app.route("/api/charging/mode", methods=["POST"])
def api_set_charge_mode():
    """POST {"mode": "最大电池电量", "start": 40, "stop": 80}"""
    d = request.get_json(force=True)
    mode = d.get("mode", "")
    if not mode:
        return jsonify({"ok": False, "error": "缺少 mode 参数"}), 400
    try:
        start = int(d.get("start", 40))
        stop = int(d.get("stop", 80))
        set_charge_mode(mode, start, stop)
        return jsonify({"ok": True, "mode": mode, "start": start, "stop": stop})
    except DllError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/charging/status", methods=["GET"])
def api_get_charge_status():
    try:
        return jsonify(get_charge_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 灯光 ──
@app.route("/api/light/keyboard", methods=["POST"])
def api_set_keyboard_light():
    """POST {"mode": "常亮", "color": "#ff0000", "brightness": "亮度3"}"""
    d = request.get_json(force=True)
    try:
        set_keyboard_light(
            d.get("mode", "常亮"),
            d.get("color", "#ff0000"),
            d.get("brightness", "亮度3"),
        )
        return jsonify({"ok": True})
    except DllError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/light/keyboard", methods=["GET"])
def api_get_keyboard_light():
    """返回可用选项（硬件不支持回读）"""
    return jsonify({
        "available_modes": list(LIGHT_MODE_MAP.keys()),
        "available_brightness": list(BRIGHTNESS_MAP.keys()),
    })

@app.route("/api/light/led", methods=["POST"])
def api_set_led_light():
    """POST {"mode": "常亮", "color": "#00ff00", "brightness": "亮度3"}"""
    d = request.get_json(force=True)
    try:
        set_led_light(
            d.get("mode", "常亮"),
            d.get("color", "#00ff00"),
            d.get("brightness", "亮度3"),
        )
        return jsonify({"ok": True})
    except DllError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/light/led", methods=["GET"])
def api_get_led_light():
    return jsonify({
        "available_modes": list(LIGHT_MODE_MAP.keys()),
        "available_brightness": list(BRIGHTNESS_MAP.keys()),
    })

@app.route("/api/light/auto", methods=["POST"])
def api_set_auto_light():
    """POST {"enabled": bool}"""
    d = request.get_json(force=True)
    try:
        set_auto_light(bool(d.get("enabled", False)))
        return jsonify({"ok": True})
    except DllError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Win 键锁 ──
@app.route("/api/win_lock", methods=["GET"])
def api_get_win_lock():
    return jsonify({"win_lock": get_win_lock()})

@app.route("/api/win_lock", methods=["POST"])
def api_set_win_lock():
    """POST {"enabled": bool}"""
    d = request.get_json(force=True)
    try:
        set_win_lock(bool(d.get("enabled", False)))
        return jsonify({"ok": True, "win_lock": get_win_lock()})
    except DllError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 传感器 ──
@app.route("/api/sensors", methods=["GET"])
def api_get_sensors():
    """GET /api/sensors?type=temperature"""
    st = request.args.get("type", "").lower() or None
    return jsonify({"sensors": get_lhm_sensors(sensor_type=st)})

@app.route("/api/sensors/temperatures", methods=["GET"])
def api_get_lhm_temperatures():
    return jsonify({"sensors": get_lhm_temperatures()})


# ── 机型 ──
@app.route("/api/model/info", methods=["GET"])
def api_notebook_info():
    return jsonify(get_notebook_info())

@app.route("/api/model/capabilities", methods=["GET"])
def api_model_capabilities():
    return jsonify({"capabilities": get_model_capabilities()})


# ── 综合状态快照 ──
@app.route("/api/hardware/status", methods=["GET"])
def api_hardware_status():
    """一次性获取温度、风扇、性能模式、GPU模式、充电状态"""
    result = {"dll_loaded": _dll_loaded}
    if not _dll_loaded:
        result["error"] = _dll_error or "DLL未加载"
        return jsonify(result), 503

    for key, fn, unit in [
        ("cpu_temperature", get_cpu_temperature, "Celsius"),
        ("gpu_temperature", get_gpu_temperature, "Celsius"),
        ("cpu_fan_speed", get_cpu_fan_speed, "RPM"),
        ("gpu_fan_speed", get_gpu_fan_speed, "RPM"),
    ]:
        try:
            result[key] = {"value": fn(), "unit": unit}
        except DllError as e:
            result[key] = {"error": str(e), "value": 0, "unit": unit}
        except Exception as e:
            logger.error(f"获取{key}异常: {e}")
            result[key] = {"error": str(e), "value": 0, "unit": unit}

    try:
        result["performance_mode"] = get_performance_mode()
    except DllError as e:
        result["performance_mode"] = {"error": str(e), "mode": "未知", "code": -1}
    except Exception as e:
        logger.error(f"获取性能模式异常: {e}")
        result["performance_mode"] = {"error": str(e), "mode": "未知", "code": -1}
    try:
        result["gpu_mode"] = get_gpu_mode()
    except DllError as e:
        result["gpu_mode"] = {"error": str(e), "mode": "未知", "code": -1}
    except Exception as e:
        logger.error(f"获取GPU模式异常: {e}")
        result["gpu_mode"] = {"error": str(e), "mode": "未知", "code": -1}
    result["fan_status"] = {
        "fan_mode": _ctl.current_fan_mode,
        "is_custom_mode": _ctl.is_custom_mode,
        "is_full_mode": _ctl.is_full_mode,
    }
    try:
        result["charge_status"] = get_charge_status()
    except Exception as e:
        logger.error(f"获取充电状态异常: {e}")
        result["charge_status"] = {"error": str(e), "mode": "未知", "start": 0, "stop": 0, "battery_level": 0, "ac_power": False}

    return jsonify(result)


# ── 亮度控制 ──
@app.route("/api/hardware/brightness", methods=["GET"])
def api_get_brightness():
    """获取系统亮度"""
    try:
        if _wmi:
            val = call_dll(_wmi.GetScreenBrightness, error_msg="获取亮度失败")
            return jsonify({"brightness": int(val)})
    except DllError:
        pass
    # fallback: WMI
    try:
        import wmi as wmi_mod
        c = wmi_mod.WMI(namespace='root/WMI')
        for b in c.WmiMonitorBrightness():
            if hasattr(b, 'CurrentBrightness'):
                return jsonify({"brightness": int(b.CurrentBrightness)})
    except Exception:
        pass
    return jsonify({"brightness": 80})

@app.route("/api/hardware/brightness", methods=["POST"])
def api_set_brightness():
    """设置系统亮度 POST {"brightness": 0-100}"""
    try:
        d = request.get_json(force=True)
        v = max(0, min(100, int(d.get("brightness", 80))))
        if _wmi:
            try:
                call_dll(_wmi.SetScreenBrightness, v, error_msg="设置亮度失败")
                return jsonify({"ok": True, "brightness": v})
            except DllError:
                pass
        # fallback: WMI
        try:
            import wmi as wmi_mod
            c = wmi_mod.WMI(namespace='root/WMI')
            for b in c.WmiMonitorBrightnessMethods():
                b.WmiSetBrightness(1, v)
                return jsonify({"ok": True, "brightness": v})
        except Exception:
            pass
        return jsonify({"ok": False, "error": "亮度控制不可用"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 屏幕关闭 ──
@app.route("/api/hardware/screen_off", methods=["POST"])
def api_screen_off():
    """关闭显示器（通过 Windows API）"""
    try:
        import ctypes
        from ctypes import wintypes
        HWND_BROADCAST = 0xFFFF
        WM_SYSCOMMAND = 0x0112
        SC_MONITORPOWER = 0xF170
        MONITOR_OFF = 2
        ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF)
        logger.info("屏幕已关闭")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"关闭屏幕失败: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 内存优化 ──
@app.route("/api/memory/optimize", methods=["POST"])
def api_memory_optimize():
    """内存优化（不增加虚拟内存占用，不影响游戏）

    安全策略:
      - 全屏应用检测: 检测到全屏窗口则完全跳过
      - 高负载跳过: CPU>5% 或 内存>500MB 的进程不清理
      - 不使用 MemoryEmptyWorkingSets(cmd=2): 它是系统级操作，会清空所有进程包括游戏
      - 不使用 MemoryPurgeWorkingSet(cmd=3): 会写 pagefile 增加虚拟内存
    """
    import ctypes
    import ctypes.wintypes as wintypes

    result = {'ok': True, 'before': 0, 'after': 0, 'freed': 0, 'details': []}
    try:
        _k32 = ctypes.windll.kernel32
        _psapi = ctypes.windll.psapi
        _u32 = ctypes.windll.user32
        _ntdll = ctypes.windll.ntdll

        _k32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
        _k32.OpenProcess.restype = ctypes.c_void_p
        _k32.CloseHandle.argtypes = [ctypes.c_void_p]
        _k32.CloseHandle.restype = ctypes.c_bool
        _k32.SetSystemFileCacheSize.argtypes = [ctypes.c_ssize_t, ctypes.c_ssize_t, ctypes.c_uint]
        _k32.SetSystemFileCacheSize.restype = ctypes.c_bool
        _psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        _psapi.EmptyWorkingSet.restype = ctypes.c_bool

        # 0. 全屏检测 — 在任何操作之前，检测到全屏直接返回
        try:
            fullscreen_hwnd = _u32.GetForegroundWindow()
            if fullscreen_hwnd:
                rect = wintypes.RECT()
                _u32.GetWindowRect(fullscreen_hwnd, ctypes.byref(rect))
                sw = _u32.GetSystemMetrics(0)
                sh = _u32.GetSystemMetrics(1)
                if (rect.right - rect.left >= sw - 2 and rect.bottom - rect.top >= sh - 2):
                    result['details'].append("检测到全屏应用，跳过优化")
                    try:
                        import psutil
                        result['before'] = round(psutil.virtual_memory().percent, 1)
                        result['after'] = result['before']
                    except Exception:
                        pass
                    return jsonify(result)
        except Exception:
            pass

        # 读取优化前
        try:
            import psutil
            mem = psutil.virtual_memory()
            result['before'] = round(mem.percent, 1)
            result['details'].append(f"物理: {mem.used // (1024*1024)}MB/{mem.total // (1024*1024)}MB")
        except Exception:
            pass

        # 1. 最小化系统文件缓存
        try:
            _k32.SetSystemFileCacheSize(0, 0, 0)
            result['details'].append("系统缓存已清除")
        except OSError:
            pass

        # 2. 逐进程修剪工作集（将工作集页面移到 Standby List，不写 pagefile）
        #    跳过 CPU>5% 或 内存>500MB 的进程（游戏/前台应用）
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_QUERY_INFORMATION = 0x0400
        desired_access = PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION
        try:
            import psutil
            skipped = 0
            cleaned = 0
            for proc in psutil.process_iter(['pid', 'cpu_percent', 'memory_info']):
                try:
                    info = proc.info
                    pid = info['pid']
                    if pid == 0 or pid == 4:
                        continue
                    cpu = info.get('cpu_percent', 0) or 0
                    mem_mb = (info.get('memory_info', None).rss / 1024 / 1024) if info.get('memory_info') else 0
                    if cpu > 5.0 or mem_mb > 500:
                        skipped += 1
                        continue
                    h_proc = _k32.OpenProcess(desired_access, False, pid)
                    if h_proc:
                        _psapi.EmptyWorkingSet(h_proc)
                        _k32.CloseHandle(h_proc)
                        cleaned += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            result['details'].append(f"清理: {cleaned}个进程, 跳过{skipped}个")
        except Exception:
            pass

        # 3. 清空 Standby List — 释放物理内存（不影响运行中程序）
        try:
            class SYS_MEM_LIST_INFO(ctypes.Structure):
                _fields_ = [("Command", ctypes.c_uint32)]
            info = SYS_MEM_LIST_INFO()
            info.Command = 4  # MemoryPurgeStandbyList
            _ntdll.NtSetSystemInformation(80, ctypes.byref(info), ctypes.sizeof(info))
            result['details'].append("Standby已清空")
        except OSError:
            pass

        # 4. 刷新当前进程工作集
        try:
            _psapi.EmptyWorkingSet(_k32.GetCurrentProcess())
        except OSError:
            pass

        # 5. 恢复系统文件缓存
        try:
            _k32.SetSystemFileCacheSize(-1, -1, 0)
        except OSError:
            pass

        # 读取优化后
        try:
            import psutil
            mem = psutil.virtual_memory()
            result['after'] = round(mem.percent, 1)
            result['details'].append(f"优化后: {mem.used // (1024*1024)}MB/{mem.total // (1024*1024)}MB")
        except Exception:
            pass

        result['freed'] = max(0, round(result['before'] - result['after'], 1))
        logger.info(f"[内存优化] {result['before']}% → {result['after']}% (释放 {result['freed']}%)")
    except Exception as e:
        logger.error(f"[内存优化] 执行失败: {e}")
        result['ok'] = False
        result['error'] = str(e)

    return jsonify(result)


# ── 风扇曲线保存/加载 ──
FAN_CURVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "conf", "fan_curve.json")

def _load_fan_curves():
    try:
        if os.path.exists(FAN_CURVE_FILE):
            with open(FAN_CURVE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"cpu": {}, "gpu": {}}

def _save_fan_curves(data):
    os.makedirs(os.path.dirname(FAN_CURVE_FILE), exist_ok=True)
    with open(FAN_CURVE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.route("/api/fan/curve", methods=["GET"])
def api_get_fan_curve():
    """获取风扇曲线"""
    return jsonify(_load_fan_curves())

@app.route("/api/fan/curve", methods=["POST"])
def api_save_fan_curve():
    """保存风扇曲线 POST {"cpu": {temp: speed}, "gpu": {temp: speed}}"""
    try:
        d = request.get_json(force=True)
        curves = _load_fan_curves()
        if "cpu" in d:
            curves["cpu"] = d["cpu"]
        if "gpu" in d:
            curves["gpu"] = d["gpu"]
        _save_fan_curves(curves)
        logger.info("风扇曲线已保存")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 错误处理 ──
@app.errorhandler(404)
def _not_found(e):
    return jsonify({"error": "端点不存在", "code": 404}), 404

@app.errorhandler(500)
def _internal_error(e):
    return jsonify({"error": "服务器内部错误", "code": 500}), 500


# ============================================================
# 主入口
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("IGAME_API_PORT", 5000))
    logger.info(f"iGame 硬件 API 启动 → http://0.0.0.0:{port}")
    logger.info(f"DLL: {'OK' if _dll_loaded else 'FAIL — ' + str(_dll_error)}")
    # Flask 开发服务器 — 单进程、无额外缓存/数据库
    # 生产环境建议: pip install waitress && waitress-serve --host=0.0.0.0 --port=5000 app:app
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)