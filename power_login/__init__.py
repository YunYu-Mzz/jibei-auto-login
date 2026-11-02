"""采集程序可调用的电力交易平台登录接口。"""
from .login import LoginError, LoginResult, SITES, login

__all__ = ['LoginError', 'LoginResult', 'SITES', 'login']
