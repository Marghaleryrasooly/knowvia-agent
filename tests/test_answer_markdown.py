"""Execute the shipped bounded renderer offline, including injection cases."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def render(*texts):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for frontend execution checks')
    script = (ROOT / 'src/knowvia/static/app.js').read_text(encoding='utf-8')
    escape = script[script.index('function escapeHtml('):].splitlines()[0]
    renderer = script[script.index('function renderAnswerMarkdown('):script.index('function renderMessage(')]
    # Minimal DOM text serialization; no app startup or network access.
    stub = "const document={createElement(){return {textContent:'',get innerHTML(){return String(this.textContent).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}};}};"
    result = subprocess.run([node, '-e', stub + escape + renderer +
        'console.log(JSON.stringify(' + json.dumps(texts) + '.map(renderAnswerMarkdown)));'],
        capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_bold_paragraphs_line_breaks_and_semantic_lists():
    assert render('**Important**\nNext line\n\n- First\n- **Second**\n\n1. Start\n2. Finish')[0] == (
        '<p><strong>Important</strong><br>Next line</p>'
        '<ul><li>First</li><li><strong>Second</strong></li></ul>'
        '<ol><li>Start</li><li>Finish</li></ol>')


@pytest.mark.parametrize('text', [
    '<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
    '**<svg onload=alert(1)>**', '[click](javascript:alert(1))',
    '[click](data:text/html,evil)', '<a href="https://evil.example">click</a>',
    '&lt;script&gt;alert(1)&lt;/script&gt;', '**</strong><iframe src=x>**',
])
def test_html_and_links_cannot_become_active(text):
    result = render(text)[0]
    assert not any(tag in result for tag in ['<script', '<img', '<svg', '<a ', '<iframe'])
    assert 'href=' not in result or '&lt;a ' in result


def test_users_sources_and_action_fields_remain_escaped():
    script = (ROOT / 'src/knowvia/static/app.js').read_text(encoding='utf-8')
    assert 'bubble">${escapeHtml(message.text)}' in script
    assert '${escapeHtml(s.document)}' in script
    assert '${escapeHtml(s.section)}' in script
    assert '${escapeHtml(message.action.description)}' in script
    assert '${renderAnswerMarkdown(message.text)}</div>${sources}${action}${ticket}${demo}' in script


def test_empty_and_crlf_text():
    assert render('', 'One\r\nTwo') == ['', '<p>One<br>Two</p>']
