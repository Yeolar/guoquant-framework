# -*- coding: utf-8 -*-
"""guoquant Sphinx 配置：本目录下执行

    sphinx-build -b html . _build/html     # 或 make html
"""
import os
import sys

# guoquant 包根目录（docs/ 的上一级，即 quant-framework/）
sys.path.insert(0, os.path.abspath('..'))

project = 'guoquant'
author = 'guoziapp'
copyright = '2025, guoziapp'
release = '0.1'

# 可选行情依赖（futu-api / xtquant 未安装时文档构建不中断）
# 注意：futu 的 provider 模块用 `from futu import *` 星号导入，autodoc 的
# MagicMock 无法提供具名符号（星号导入会静默为空 → 模块体 NameError），
# 因此这里用真实占位模块预置 sys.modules（仅文档构建期生效）。
import types as _types
import re as _re

if 'futu' not in sys.modules:
    _futu = _types.ModuleType('futu')

    class _Plate(object):
        """futu 板块枚举占位：文档构建期无需真实 SDK。"""

        INDUSTRY = 'industry'
        CONCEPT = 'concept'
        REGION = 'region'

        def __init__(self, *args, **kwargs):
            pass

    class _Enum(object):
        """任意枚举/常量占位：访问任意属性均返回可用的占位对象。"""

        def __init__(self, name=''):
            self._name = name

        def __getattr__(self, item):
            return _Enum(f'{self._name}.{item}')

    def _futu_getattr(name):
        return _Enum(name)

    _futu.Plate = _Plate
    _futu.OpenQuoteContext = _Enum  # futuapi/context.py 具名导入占位
    _futu.__getattr__ = _futu_getattr  # PEP 562：任意未定义符号兜底
    # 星号导入所需符号：扫描 futuapi/*.py 顶层大写标识符自动收集，
    # 覆盖现有及未来新增的枚举引用（剔除 python/第三方常见名）
    _futu_dir = os.path.join(os.path.dirname(__file__), '..',
                             'guoquant', 'common', 'fetch', 'futuapi')
    _symbols = set()
    if os.path.isdir(_futu_dir):
        for _fn in os.listdir(_futu_dir):
            if _fn.endswith('.py'):
                _text = open(os.path.join(_futu_dir, _fn),
                             encoding='utf-8').read()
                _symbols |= set(_re.findall(r'\b[A-Z][A-Za-z0-9_]*\b', _text))
    _symbols -= {'DataFrame', 'Exception', 'False', 'None', 'True',
                 'Path', 'JSON', 'CSV', 'OrderedDict', 'Progress'}
    _futu.__all__ = sorted(_symbols)
    sys.modules['futu'] = _futu

autodoc_mock_imports = ['xtquant']

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
]

# 从 docstring 提取内容（Google / numpy 风格均可解析，本项目以中文 prose 为主）
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_private_with_doc = False
# 对象（函数/类/方法）展示不带模块路径前缀（分模块页面本身已含上下文）
add_module_names = False

autodoc_default_options = {
    'members': True,
    'undoc-members': False,
    'show-inheritance': True,
}


templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

language = 'zh_CN'

html_theme = 'furo'
html_css_files = []
html_static_path = ['_static']

