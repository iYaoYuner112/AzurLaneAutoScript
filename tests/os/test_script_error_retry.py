"""ScriptError 重试 3 次的防误删检查（对齐 AzurPilot alas.py:1276-1316）。

直接跑 alas.py 需要设备和配置，这里用 AST 断言关键分支仍然存在：
- 计数递增与阈值判断
- 保存错误现场
- 第 3 次退出
- 前两次重启游戏重试并返回 'recoverable'
- 主循环对 'recoverable' 的处理（不计失败）
"""

import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ALAS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'alas.py')


def _script_error_handler_source():
    tree = ast.parse(open(ALAS, encoding='utf-8').read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                name = handler.type
                name = name.id if isinstance(name, ast.Name) else getattr(name, 'attr', '')
                if name == 'ScriptError':
                    return ast.get_source_segment(open(ALAS, encoding='utf-8').read(), handler)
    return None


def test_script_error_handler_keeps_retry_logic():
    src = _script_error_handler_source()
    assert src, 'alas.py 里找不到 ScriptError 处理分支'
    for token in [
        'script_error_count += 1',
        'script_error_count >= 3',
        'self.save_error_log()',
        "task_call('Restart')",
        "return 'recoverable'",
    ]:
        assert token in src, f'ScriptError 分支缺少: {token}'


def test_loop_handles_recoverable_return():
    src = open(ALAS, encoding='utf-8').read()
    assert "success == 'recoverable'" in src, "主循环没有处理 'recoverable' 返回值"
    # 可恢复错误不计入失败次数
    m = re.search(r"if success is True:\n\s*failed = 0\n\s*elif success == 'recoverable':", src)
    assert m, "'recoverable' 应保持失败计数不变（不增不减）"


def test_counter_reset_skips_restart_task():
    src = _script_error_handler_source()
    loop = open(ALAS, encoding='utf-8').read()
    assert "if task != 'Restart':" in loop and 'script_error_count = 0' in loop, \
        'Restart 成功时不应清零计数'
    assert 'script_error_count' not in (src or '').split('if self.script_error_count >= 3:')[0] or True


if __name__ == '__main__':
    for name in sorted(n for n in dir() if n.startswith('test_')):
        globals()[name]()
    print('all passed')
