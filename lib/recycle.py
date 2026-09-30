# -*- coding: utf-8 -*-
"""
回收站删除（共用）
==========================================================
本项目铁律：不"删除"文件，只把它丢进回收站（可右键还原）。
走 Windows Shell 的 SHFileOperationW + FOF_ALLOWUNDO，
不碰 os.remove / shutil.rmtree。

注意：本机实测该 API 经常返回 2（并非 0=成功），但文件其实已经进了回收站。
所以调用方**不要只看返回值**，要看"文件是否真的消失"——
本函数已经替你做完这件事：返回"仍然存在"的路径列表，空列表 = 全部成功。
"""
import ctypes
import os


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_void_p),
                ("pTo", ctypes.c_void_p),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", ctypes.c_bool),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


def 移入回收站(paths):
    """把 paths 里的文件 / 文件夹丢进回收站。

    返回「仍然存在」的路径列表（空列表 = 全部成功进回收站）。
    传入不存在的路径会被自动忽略，不算失败。
    """
    存在 = [os.path.abspath(p) for p in paths if os.path.exists(p)]
    if not 存在:
        return []

    FO_DELETE = 3
    FOF_SILENT = 0x4
    FOF_NOCONFIRMATION = 0x10
    FOF_ALLOWUNDO = 0x40
    FOF_NOERRORUI = 0x400

    buf = ctypes.create_unicode_buffer("\0".join(存在) + "\0\0")
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
    op.pTo = None
    op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI
    ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return [p for p in 存在 if os.path.exists(p)]
