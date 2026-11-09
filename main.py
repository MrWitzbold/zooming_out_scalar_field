import os
import socket
import unicodedata
import importlib.util
import traceback
from datetime import datetime
from flask import Flask, request, render_template_string, url_for, Response, send_file
from pdf_search import search_pdfs
from flask import send_from_directory
from pdf2image import convert_from_path
import img2pdf
from PyPDF2 import PdfMerger
from PIL import Image
import tempfile
from flask import jsonify
import zipfile
from mistralai.client import Mistral
import json
import pywhatkit
import re
import pyautogui
import time

# O relatório dentro da pasta desse .py deve ser impresso de consultas > estudantes matriculado > ativos > telefones modelo II

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

print("API KEY:", MISTRAL_API_KEY)

app = Flask(__name__)

messages = []

def askmistral(text):
    client = Mistral(api_key='pqIWHQB2bHVnAJn7blIHaErF7bAlq4Dj')
    context_string = ""
    initial_instruction = ("Seu nome é Lagora. Você é uma IA assistente virtual que ajuda "
                          "auxiliares administrativos com processos escolares. Respostas curtas, "
                          "a não ser que seja pedido listas etc... Tenha personalidade 1337, "
                          "mas sem larping nem roleplay.")
    if context_string.strip():
        messages.append({"role": "system", "content": context_string.strip()})
    messages.append({"role": "system", "content": initial_instruction})
    messages.append({"role": "user", "content": text})

    response = client.chat.complete(model="open-mistral-7b", messages=messages)
    return response

def send_message(whatsnum, text):
    now = datetime.now()
    pywhatkit.sendwhatmsg(whatsnum, text, now.hour, now.minute + 3)
    time.sleep(20)
    print("pressing enter")
    pyautogui.press("enter")

def text_to_mistral_jsonl(text: str):
    pattern = re.compile(
        r"Name:\s*(.*?)\nAge:\s*(\d+)\nCohort:\s*(.*?)(?:\n\n|\Z)",
        re.DOTALL
    )
    lines = []
    for name, age, cohort in pattern.findall(text):
        user_question = f"What is the cohort and age of {name.strip()}?"
        assistant_answer = f"{name.strip()} is {age} years old and is in {cohort.strip()}."
        entry = {"messages": [
            {"role": "user", "content": user_question},
            {"role": "assistant", "content": assistant_answer}
        ]}
        lines.append(json.dumps(entry, ensure_ascii=False))
    return "\n".join(lines)

def write_data_file(thing):
    with open("data.json", "w", encoding="utf-8") as f:
        f.write(thing)

def students_to_string(students):
    lines = []
    for student in students:
        lines.append("\n".join([
            f"Name: {student.name}",
            f"Age: {student.age}",
            f"Cohort: {student.cohort}",
        ]))
    return "\n\n".join(lines)

# =========================================================================
# =========================================================================
#  PLUGIN SYSTEM  —  carrega ferramentas da pasta plugins/
# =========================================================================
# =========================================================================
#
#  Cada arquivo .py dentro de plugins/ que NÃO comece com "_"
#  é carregado automaticamente como uma nova seção do painel.
#
#  Campos OBRIGATÓRIOS do plugin:
#     SECTION_ID  (str)   identificador único → rota /ferramentas/<id>
#     LABEL       (str)   nome exibido no menu
#     handle(req)         função que recebe Flask request e retorna dict
#
#  Campos OPCIONAIS:
#     ICON        (str)   emoji do menu (default: '🔧')
#     DESCRIPTION (str)   subtítulo da seção
#     FIELDS_HTML (str)   apenas os <input>/<label>. O loader gera o <form>
#                         completo com botão "Enviar" automaticamente.
#     FORM_HTML   (str)   HTML de um <form> completo — DEVE ter
#                         id="form_plugin_<SECTION_ID>"
#     INFO_HTML   (str)   HTML estático exibido ANTES do formulário
#
#  Retorno do handle(request):
#     {"html": "<p>Resultado</p>"}
#     {"html": "...", "download": {"content": "...", "filename": "out.txt"}}
#     {"download": {"content": b64, "encoding": "base64",
#                   "mimetype": "application/zip", "filename": "out.zip"}}
#     {"error": "mensagem de erro"}
# =========================================================================

PLUGINS_DIR = "plugins"

PLUGIN_TEMPLATE = '''"""
TEMPLATE DE PLUGIN — copie este arquivo, renomeie para <seu_id>.py,
edite os campos e ele será carregado automaticamente como uma seção.

Campos obrigatórios:
    SECTION_ID  (str)  identificador único, usado na URL /ferramentas/<id>
    LABEL       (str)  nome exibido no menu
    handle(request)    função que processa o request e retorna dict

Campos opcionais:
    ICON        (str)  emoji do menu (default: '🔧')
    DESCRIPTION (str)  subtítulo da seção
    FIELDS_HTML (str)  apenas os campos do formulário (inputs/labels).
                       O sistema gera <form id="form_plugin_<id>"> com botão.
    FORM_HTML   (str)  HTML completo de um <form>, se quiser controle total.
                       DEVE ter id="form_plugin_<id>".
    INFO_HTML   (str)  HTML estático exibido antes do formulário.

O handle(request) recebe o request do Flask (request.files, request.form, etc.)
e deve retornar um dos formatos:
    {"html": "<p>Resultado</p>"}
    {"html": "...", "download": {"content": "...", "filename": "out.txt"}}
    {"download": {"content": b64, "encoding": "base64",
                  "mimetype": "application/zip", "filename": "out.zip"}}
    {"error": "mensagem"}

Exemplo abaixo — conte linhas de um .txt
"""
SECTION_ID = "exemplo"
LABEL = "Exemplo"
ICON = "🧪"
DESCRIPTION = "Plugin de exemplo — conta linhas do arquivo enviado"

FIELDS_HTML = """
<label>Envie um arquivo .txt</label>
<input type="file" name="file" accept=".txt" required class="file-input">
"""

def handle(request):
    f = request.files.get("file")
    if not f:
        return {"error": "Nenhum arquivo enviado."}
    content = f.read().decode("utf-8", errors="ignore")
    count = len(content.splitlines())
    return {
        "html": f"<p><strong>{count}</strong> linha(s) no arquivo.</p>",
        "download": {
            "content": f"Total de linhas: {count}\\n",
            "filename": "contagem.txt",
        },
    }
'''

def _load_plugins():
    """Varre plugins/ e importa todos os .py válidos."""
    plugins = {}

    # cria a pasta e o template inicial se necessário
    if not os.path.isdir(PLUGINS_DIR):
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        print(f"[PLUGIN] pasta '{PLUGINS_DIR}/' criada")

    py_files = [f for f in os.listdir(PLUGINS_DIR)
                if f.endswith('.py') and not f.startswith('_')]

    if not py_files:
        template_path = os.path.join(PLUGINS_DIR, '_exemplo.py')
        if not os.path.exists(template_path):
            with open(template_path, 'w', encoding='utf-8') as f:
                f.write(PLUGIN_TEMPLATE)
            print(f"[PLUGIN] template criado em {template_path} "
                  f"(copie e renomeie para criar sua ferramenta)")

    for fname in sorted(os.listdir(PLUGINS_DIR)):
        if not fname.endswith('.py') or fname.startswith('_'):
            continue
        path = os.path.join(PLUGINS_DIR, fname)
        module_name = f"_plugin_{fname[:-3]}"

        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"[PLUGIN] ERRO importando {fname}: {e}")
            traceback.print_exc()
            continue

        sid = getattr(mod, 'SECTION_ID', None)
        if not sid:
            print(f"[PLUGIN] {fname} sem SECTION_ID — ignorado")
            continue
        if sid in plugins:
            print(f"[PLUGIN] SECTION_ID duplicado '{sid}' ({fname}) — ignorado")
            continue

        # Se tem FIELDS_HTML mas não FORM_HTML, gera o <form> em volta
        form_html = getattr(mod, 'FORM_HTML', '')
        if not form_html:
            fields = getattr(mod, 'FIELDS_HTML', '')
            if fields:
                form_html = (
                    f'<form id="form_plugin_{sid}" enctype="multipart/form-data" '
                    f'class="tool-form">\n{fields}\n'
                    f'<button type="submit" class="btn btn-primary">Enviar</button>\n'
                    f'</form>'
                )

        plugins[sid] = {
            'id': sid,
            'label': getattr(mod, 'LABEL', sid.replace('_', ' ').title()),
            'icon': getattr(mod, 'ICON', '🔧'),
            'description': getattr(mod, 'DESCRIPTION', ''),
            'form_html': form_html,
            'info_html': getattr(mod, 'INFO_HTML', ''),
            'handle': getattr(mod, 'handle', None),
        }
        print(f"[PLUGIN] carregado: {sid}  ←  {fname}")

    return plugins

PLUGINS = _load_plugins()
print(f"[PLUGIN] total carregado: {len(PLUGINS)}")
# =========================================================================
# =========================================================================

# =========================================================================
# HTML TEMPLATE
# =========================================================================
HTML_TEMPLATE = """
<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<link rel="icon" href="https://icon.99helpers.site/icon.ico" type="image/x-icon">
<title>Lagora — Peridot Hub</title>
<style>
  /* =========================================================
     DESIGN TOKENS — tema Flandre Scarlet / blood-moon crimson
     Para mudar cores/tema, edite apenas este bloco.
     ========================================================= */
  :root {
    /* Backgrounds — pálidos, rosados, tingidos de sangue */
    --bg: #FFE8EC;
    --bg-alt: #FFD9E0;
    --panel: #FFF5F7;
    --glass: rgba(255, 235, 240, 0.9);

    /* Núcleo do tema — vermelho carmesim / scarlet */
    --neon: #C41E3A;      /* carmesim profundo (Flandre dress) */
    --neon-2: #E63946;    /* vermelho escarlate brilhante */
    --accent: #FFB3C1;    /* rosa suave / crystal shine */
    --muted: #7A4A52;     /* marrom-rosado acinzentado */
    --text: #2A1418;      /* marrom-avermelhado quase preto */

    /* Semânticas — ainda legíveis, mas tematizadas */
    --danger: #FF1744;    /* alarme escarlate */
    --warning: #D4A017;   /* dourado dos olhos de Flandre */
    --success: #2E7D32;   /* verde profundo, mantido para clareza */

    --radius: 16px;
    --radius-sm: 10px;
    --radius-pill: 999px;

    /* Sombras — tingidas de vinho em vez de azul */
    --shadow-sm: 0 2px 8px rgba(120, 20, 40, 0.08);
    --shadow: 0 8px 30px rgba(120, 20, 40, 0.10);
    --shadow-lg: 0 20px 50px rgba(120, 20, 40, 0.15);

    font-family: 'Inter', 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }

  body {
    /* Aura de lua-de-sangue: dois glows radiais em carmesim */
    background:
      radial-gradient(1200px 600px at 10% 10%, rgba(230, 57, 70, 0.20), transparent 45%),
      radial-gradient(900px 500px at 90% 80%, rgba(196, 30, 58, 0.10), transparent 45%),
      var(--bg);
    color: var(--text);
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    padding: 24px;
    display: flex;
    justify-content: center;
    font-size: 15px;
    line-height: 1.5;
    min-height: 100vh;
  }

  body::before {
    content: "";
    position: fixed;
    inset: 0;
    z-index: -1;
    background: url("https://i.ibb.co/LzwvrL23/Biblioteca-G-tica-dos-Cristais-Flutuantes.png") center/cover repeat;
    opacity: 0.55;
    /* Aquece o gif para combinar com o tema vinho */
    filter: brightness(1.25) saturate(0.85) sepia(0.2);
    pointer-events: none;
  }

  /* scrollbar — carmesim com brilho */
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: rgba(42, 20, 24, 0.15); }
  ::-webkit-scrollbar-thumb {
    background: linear-gradient(180deg, var(--neon-2), var(--neon));
    border-radius: 10px;
    box-shadow: 0 0 10px var(--neon-2);
  }
  ::-webkit-scrollbar-thumb:hover {
    background: linear-gradient(180deg, var(--neon), var(--accent));
  }
  html { scrollbar-width: thin; scrollbar-color: var(--neon-2) rgba(42, 20, 24, 0.15); }

  a { color: var(--neon); text-decoration: none; font-weight: 500; }
  a:hover { text-decoration: underline; }

  /* =========================================================
     LAYOUT
     ========================================================= */
  .wrap {
    width: 100%;
    max-width: 1400px;
    display: grid;
    grid-template-columns: 290px 1fr;
    gap: 24px;
    align-items: start;
  }

  /* =========================================================
     SIDEBAR
     ========================================================= */
  .sidebar {
    background: linear-gradient(180deg, #FFD9E0, #FFF0F3);
    border-radius: var(--radius);
    padding: 20px;
    box-shadow: var(--shadow-lg);
    position: sticky;
    top: 24px;
    height: calc(100vh - 48px);
    overflow: auto;
    border: 1px solid rgba(230, 57, 70, 0.08);
    backdrop-filter: blur(8px);
  }

  .brand { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }
  .brand a { display: inline-block; line-height: 0; }
  .brand img {
    width: 54px; height: 54px; border-radius: 12px;
    border: 1px solid rgba(255, 240, 245, 0.9);
    box-shadow: 0 6px 20px rgba(196, 30, 58, 0.14), 0 2px 6px rgba(0, 0, 0, 0.4);
  }
  .brand h1 { font-size: 17px; color: var(--neon-2); letter-spacing: 0.3px; }
  .brand p { font-size: 11px; color: var(--muted); }

  .peridot-thumb {
    width: 100%;
    height: 130px;
    object-fit: cover;
    border-radius: var(--radius-sm);
    margin: 8px 0 14px;
    opacity: 0.95;
    border: 1px solid rgba(230, 57, 70, 0.08);
  }

  .menu { display: flex; flex-direction: column; gap: 2px; margin-top: 6px; }
  .menu a {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 12px;
    border-radius: var(--radius-sm);
    color: var(--text);
    font-weight: 600;
    font-size: 14px;
    transition: all 0.15s ease;
    background: transparent;
    border: 1px solid transparent;
  }
  .menu a:hover {
    transform: translateX(4px);
    background: linear-gradient(90deg, rgba(196, 30, 58, 0.08), rgba(230, 57, 70, 0.04));
    border-color: rgba(196, 30, 58, 0.10);
    color: var(--neon);
    text-decoration: none;
  }
  .menu a.active {
    background: linear-gradient(90deg, rgba(196, 30, 58, 0.14), rgba(230, 57, 70, 0.08));
    border-color: rgba(196, 30, 58, 0.22);
    color: var(--neon);
  }
  .menu-icon { font-size: 16px; opacity: 0.85; }

  .sidebar-footer {
    font-size: 12px;
    color: var(--muted);
    line-height: 1.55;
    margin-top: 14px;
    padding-top: 14px;
    border-top: 1px solid rgba(196, 30, 58, 0.10);
  }

  /* =========================================================
     MAIN
     ========================================================= */
  .main {
    padding: 24px;
    border-radius: var(--radius);
    background: rgba(255, 240, 245, 0.75);
    box-shadow: var(--shadow-lg);
    min-height: calc(100vh - 48px);
    border: 1px solid rgba(230, 57, 70, 0.06);
    position: relative;
    overflow: hidden;
  }
  .main::after {
    content: "";
    position: absolute;
    right: -60px; bottom: -20px;
    width: 420px; height: 420px;
    background: url('https://i.ibb.co/XfzgFz8m/3dgifmaker32384.gif') no-repeat center/contain;
    opacity: 0.7;
    transform: rotate(10deg);
    pointer-events: none;
    /* Leve tom avermelhado na ilustração, mantém URL intacta */
    filter: sepia(0.3) hue-rotate(-25deg) saturate(1.2);
  }

  .view { position: relative; z-index: 1; }
  .view.hidden { display: none; }

  .page-header { margin-bottom: 20px; }
  .page-header h2 {
    font-size: 22px;
    color: var(--neon);
    letter-spacing: -0.2px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .page-header p { color: var(--muted); font-size: 14px; margin-top: 4px; }

  /* stack for section content */
  .stack { display: flex; flex-direction: column; gap: 16px; }

  /* =========================================================
     CARDS
     ========================================================= */
  .card {
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.75), rgba(255, 217, 224, 0.5));
    border: 1px solid rgba(196, 30, 58, 0.10);
    border-radius: var(--radius);
    padding: 18px 20px;
    box-shadow: var(--shadow-sm);
    backdrop-filter: blur(6px);
  }
  .card-title {
    font-size: 14px;
    font-weight: 700;
    color: var(--neon);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  /* =========================================================
     INFO TABLES
     ========================================================= */
  .info-table { width: 100%; border-collapse: collapse; }
  .info-table tr { border-bottom: 1px solid rgba(196, 30, 58, 0.08); }
  .info-table tr:last-child { border-bottom: none; }
  .info-table td { padding: 9px 4px; vertical-align: middle; font-size: 14px; }
  .info-table td.label {
    color: var(--neon);
    font-weight: 600;
    width: 40%;
    padding-right: 16px;
  }
  .info-table td.value { color: var(--text); }

  /* credentials */
  .cred {
    display: inline-block;
    padding: 3px 8px;
    background: rgba(196, 30, 58, 0.08);
    border-radius: 6px;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 12.5px;
    color: var(--neon);
    margin-right: 6px;
    word-break: break-all;
  }
  .copy-group { display: inline-flex; gap: 4px; flex-wrap: wrap; }

  /* lists */
  .list { padding-left: 20px; }
  .list li { margin: 6px 0; font-size: 14px; }
  .list li strong { color: var(--neon); }
  .list.numbered { list-style: decimal; }

  /* =========================================================
     BUTTONS
     ========================================================= */
  .btn {
    padding: 10px 16px;
    border-radius: var(--radius-sm);
    border: 1px solid transparent;
    cursor: pointer;
    font-weight: 700;
    font-size: 14px;
    transition: transform 0.1s ease, box-shadow 0.15s ease, background 0.15s ease;
    font-family: inherit;
  }
  .btn:active { transform: translateY(1px); }
  .btn-primary {
    background: linear-gradient(180deg, var(--neon), #7A0E1F);
    color: #ffffff;
    box-shadow: 0 4px 14px rgba(196, 30, 58, 0.30);
  }
  .btn-primary:hover { box-shadow: 0 6px 20px rgba(196, 30, 58, 0.45); }
  .btn-amber {
    background: linear-gradient(180deg, #E8B84B, #A87A15);
    color: #2A1418;
  }
  .btn-danger {
    background: linear-gradient(180deg, #FF1744, #B00020);
    color: #ffffff;
  }
  .btn-copy {
    background: rgba(196, 30, 58, 0.08);
    color: var(--neon);
    border: 1px solid rgba(196, 30, 58, 0.18);
    padding: 4px 9px;
    font-size: 11.5px;
    font-weight: 600;
  }
  .btn-copy:hover { background: rgba(196, 30, 58, 0.16); }
  .btn-copy:disabled { opacity: 0.7; }
  .mini { padding: 6px 10px; font-size: 12.5px; border-radius: 8px; }

  .btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }

  /* =========================================================
     FORMS
     ========================================================= */
  .search-input,
  .form-input,
  input[type="text"],
  input[type="search"] {
    padding: 12px 14px;
    border-radius: var(--radius-sm);
    border: 1px solid rgba(196, 30, 58, 0.20);
    background: #FFE8EC;
    color: var(--text);
    width: 100%;
    font-size: 15px;
    font-family: inherit;
    outline: none;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  .search-input:focus,
  .form-input:focus,
  input[type="text"]:focus,
  input[type="search"]:focus {
    border-color: var(--neon);
    box-shadow: 0 0 0 3px rgba(196, 30, 58, 0.15);
  }
  input::placeholder { color: #9A7078; }

  .file-input {
    padding: 12px;
    border-radius: var(--radius-sm);
    border: 1px dashed rgba(196, 30, 58, 0.28);
    background: rgba(196, 30, 58, 0.04);
    color: var(--text);
    width: 100%;
    margin-bottom: 10px;
    cursor: pointer;
    font-family: inherit;
    font-size: 13px;
  }

  .tool-form { display: flex; flex-direction: column; gap: 4px; }
  .form-title {
    font-size: 15px;
    color: var(--neon);
    margin-bottom: 12px;
    font-weight: 700;
  }

  /* =========================================================
     HOME VIEW
     ========================================================= */
  .home-grid {
    display: grid;
    grid-template-columns: 1fr 340px;
    gap: 20px;
    align-items: start;
  }

  .logo-large {
    display: block;
    margin: 0 auto 12px;
    width: 160px;
    height: 160px;
    border-radius: var(--radius);
    object-fit: cover;
    border: 1px solid rgba(196, 30, 58, 0.12);
    box-shadow: var(--shadow-sm);
  }
  .note-title {
    font-size: 14px;
    color: var(--neon);
    font-weight: 700;
    margin-bottom: 10px;
  }

  .notes-list { margin-top: 12px; display: flex; flex-direction: column; gap: 6px; }
  .notes-list p {
    background: #FFE8EC;
    padding: 9px 12px;
    border-radius: var(--radius-sm);
    font-size: 13px;
    color: var(--text);
    border-left: 3px solid var(--neon-2);
    word-break: break-word;
  }

  /* AI box */
  .ai-box {
    margin-top: 18px;
    padding: 14px;
    background: rgba(196, 30, 58, 0.04);
    border-radius: var(--radius);
    border: 1px solid rgba(196, 30, 58, 0.12);
  }
  .ai-output {
    max-height: 260px;
    overflow-y: auto;
    margin-bottom: 10px;
    padding: 8px 10px;
    background: rgba(255, 255, 255, 0.55);
    border-radius: var(--radius-sm);
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 13px;
    min-height: 60px;
  }
  .ai-output:empty::before {
    content: "✨ Lagora está pronta para ajudar.";
    color: var(--muted);
    font-family: inherit;
  }
  .ai-output > div { padding: 3px 0; line-height: 1.5; }
  .ai-output > div > div { padding: 3px 0; }
  .ai-input-row { display: flex; gap: 8px; }
  .ai-input-row input { flex: 1; }

  /* results */
  .results { display: flex; flex-direction: column; gap: 10px; margin-top: 16px; }
  .results-title {
    color: var(--neon);
    font-size: 15px;
    margin-bottom: 4px;
  }
  .student {
    padding: 14px 16px;
    border-radius: var(--radius-sm);
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.75), rgba(255, 232, 236, 0.4));
    border: 1px solid rgba(196, 30, 58, 0.10);
    font-size: 14px;
    line-height: 1.6;
  }
  .student strong { color: var(--neon); }

  /* code box — terminal carmesim (Flandre's dark wings vibe) */
  .code-box {
    background: #1A0508;
    padding: 14px;
    border-radius: var(--radius-sm);
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 12.5px;
    color: #F5C9D0;
    overflow: auto;
    margin-top: 10px;
  }
  .code-box pre { margin: 0; }
  .code-box code { color: inherit; background: none; }

  /* =========================================================
     RESPONSIVE
     ========================================================= */
  @media (max-width: 980px) {
    .wrap { grid-template-columns: 1fr; }
    .sidebar { position: relative; top: 0; height: auto; order: 2; }
    .main { order: 1; min-height: auto; }
    .home-grid { grid-template-columns: 1fr; }
    .main::after { display: none; }
    body { padding: 12px; }
  }
</style>
</head>
<body>
<div class="wrap">

  <!-- ============================================================
       SIDEBAR
       ============================================================ -->
  <aside class="sidebar" aria-label="menu lateral">
    <div class="brand">
      <a href="/">
        <img src="https://i.ibb.co/TqWtQx7X/1.png" alt="logo peridot">
      </a>
      <div>
        <h1>Lagora Hub</h1>
        <p>Peridot — painel administrativo</p>
      </div>
    </div>

    <img class="peridot-thumb"
         src="https://i.ibb.co/TBKDCBSd/Flandre-na-Biblioteca-Arcana-Neon.png"
         alt="peridot thumb">

    <nav class="menu" id="menu" aria-label="navegação"></nav>

    <div class="sidebar-footer">
      <strong>Contatos:</strong><br>
      sargentoraymundo@edu.viamao.rs.gov.br<br>
      INEP: 43167853<br>
      Endereço: R. Padre Cacique, 265 — Viamão, RS
    </div>
  </aside>

  <!-- ============================================================
       MAIN
       ============================================================ -->
  <main class="main" role="main">

    <!-- -------------------------------------------------------
         HOME VIEW (buscar alunos / turma)
         ------------------------------------------------------- -->
    <section id="homeView" class="view">

      <div class="page-header" style="display:flex; align-items:center; gap:16px;">
        <div style="flex:1">
          <h2>🏠 Painel — Buscar Aluno / Turma</h2>
          <p>Digite o nome do aluno ou a turma (ex: J2A, 31). Pressione Enter ou clique em Buscar.</p>
        </div>
        <img src="https://cdn.pixabay.com/photo/2014/04/03/11/47/alien-312229_1280.png"
             alt="alien icon" style="height:44px;opacity:0.9">
      </div>

      <div class="home-grid">

        <!-- LEFT -->
        <div class="stack" style="gap:16px">

          <div class="card">
            <!-- IMPORTANT: preserva form/action/inputs originais -->
            <form method="GET" id="searchForm">
              <input type="search"
                     name="query"
                     id="queryInput"
                     placeholder="Nome ou Turma (ex: J2A, 31)"
                     autofocus
                     autocomplete="off"
                     class="search-input">

              <div class="btn-row">
                <input type="submit" name="action" value="Buscar" class="btn btn-primary mini">
                <input type="submit" name="action" value="Exportar dados da turma"
                       id="exportar" class="btn btn-amber mini">
                <input type="submit" name="action" value="Exportar arquivo de contatos"
                       id="exportar_contatos" class="btn btn-amber mini">
                <input type="submit" name="action" value="Procurar nas atas 2021 - 1988"
                       id="pesquisar_astas" class="btn btn-danger mini">
              </div>
            </form>
          </div>

          <!-- AI box -->
          <div class="ai-box">
            <div class="ai-output" id="lagora-ai-output"></div>
            <div class="ai-input-row">
              <input id="lagora-ai-input"
                     placeholder="✨ Me pergunte qualquer coisa..."
                     class="search-input">
              <button id="lagora-ai-send" type="button" class="btn btn-primary">Enviar</button>
            </div>
          </div>

          <!-- Results (renderizado pelo Jinja) -->
          <div class="results" id="serverResults">
            {% if results %}
              <h4 class="results-title">Resultados</h4>

              {% if mode == 'search' %}
                <p><strong>{{ results|length }} alunos encontrados.</strong></p>
                {% for s in results %}
                  <div class="student">
                    <strong>Nome:</strong> {{ s.name }}<br>
                    <strong>Turma:</strong> {{ s.cohort }}<br>
                    <strong>Idade:</strong> {{ s.age }}<br>
                    <strong>Nascimento:</strong> {{ s.birth }}<br>
                    <strong>Números:</strong>
                    {% for phone in s.phones.split(',') %}
                      {% set cleaned = phone | replace('(', '') | replace(')', '') | replace(' ', '') %}
                      <a href="https://web.whatsapp.com/send/?phone=55{{ cleaned }}" target="_blank">{{ phone.strip() }}</a>{% if not loop.last %}, {% endif %}
                    {% endfor %}
                  </div>
                {% endfor %}

              {% elif mode == 'export' %}
                {% for line in results %}
                  <div class="student">{{ line | safe }}</div>
                {% endfor %}

              {% elif mode == 'atas' %}
                {% for line in results %}
                  <div class="student">{{ line | safe }}</div>
                {% endfor %}
              {% endif %}
            {% endif %}
          </div>
        </div>

        <!-- RIGHT (anotações + logo) -->
        <aside class="stack" style="gap:16px">
          <div class="card">
            <img class="logo-large" src="https://i.ibb.co/BHghwpLp/Chibi-Hipnotizada-com-Cristais-e-Cora-es.png" alt="logo">
            <div class="note-title">Anotações rápidas</div>

            <form id="noteForm" onsubmit="return saveNote(event)">
              <input type="text" id="note" name="note" placeholder="Anotação rápida">
              <div class="btn-row" style="margin-top:8px">
                <input type="submit" class="btn btn-primary mini" value="Enviar">
                <button type="button" class="btn mini" onclick="clearNotes()"
                        style="background:rgba(0,0,0,0.04);border:1px solid rgba(0,0,0,0.08)">Limpar</button>
              </div>
            </form>

            <div class="notes-list" id="notes"></div>
          </div>

          <div class="card">
            <div class="note-title">💡 Dicas rápidas</div>
            <p style="font-size:13px;color:var(--muted)">
              Clique nos números de telefone nos resultados para abrir uma nova conversa no WhatsApp.
            </p>
          </div>
        </aside>

      </div>
    </section>

    <!-- -------------------------------------------------------
         CONTENT VIEW (seções dinâmicas — preenchido pelo JS)
         ------------------------------------------------------- -->
    <section id="contentView" class="view hidden"></section>

  </main>
</div>

<!-- ================================================================
     JS — SISTEMA DE SEÇÕES
     ================================================================ -->
<script>
/* ==================================================================
 * HELPERS — funções utilitárias para montar HTML de forma limpa
 * ================================================================== */
const h = {
  esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  },

  stack(...parts) {
    return `<div class="stack">${parts.join('')}</div>`;
  },

  header(title, subtitle = '') {
    return `
      <div class="page-header">
        <h2>${title}</h2>
        ${subtitle ? `<p>${subtitle}</p>` : ''}
      </div>`;
  },

  card(inner, title = '') {
    return `
      <div class="card">
        ${title ? `<div class="card-title">${title}</div>` : ''}
        ${inner}
      </div>`;
  },

  table(title, rows) {
    const body = rows.map(([label, value]) =>
      `<tr><td class="label">${label}</td><td class="value">${value}</td></tr>`
    ).join('');
    return `
      <div class="card">
        ${title ? `<div class="card-title">${title}</div>` : ''}
        <table class="info-table"><tbody>${body}</tbody></table>
      </div>`;
  },

  link(name, url) {
    return `<a href="${url}" target="_blank" rel="noopener">${name} ↗</a>`;
  },

  copyBtn(label, value) {
    return `<button type="button" class="btn btn-copy copy-btn"
              data-copy="${h.esc(value)}"
              data-default-value="${h.esc(label)}">${label}</button>`;
  },

  cred(display, buttons) {
    const btns = buttons.map(([label, value]) => h.copyBtn(label, value)).join('');
    return `<code class="cred">${display}</code><span class="copy-group">${btns}</span>`;
  },

  // row de senha: retorna array para usar direto em h.table
  passRow(label, display, buttons) {
    return [label, h.cred(display, buttons)];
  },

  list(items, ordered = false) {
    const tag = ordered ? 'ol' : 'ul';
    const cls = ordered ? 'list numbered' : 'list';
    return `<${tag} class="${cls}">${items.map(i => `<li>${i}</li>`).join('')}</${tag}>`;
  },
};

/* ==================================================================
 * SECTIONS — registro central de todas as seções do app
 * ------------------------------------------------------------------
 * Para adicionar uma nova seção:
 *   1. Adicione uma entrada abaixo com { label, icon, render }
 *   2. O menu e o roteamento são gerados automaticamente
 *
 * Exemplo (estágios probatórios):
 *   estagios: {
 *     label: 'Estágios probatórios',
 *     icon: '📅',
 *     render: () => h.stack(
 *       h.header('Estágios probatórios', 'Datas dos estágios'),
 *       h.table('Funcionários', [
 *         ['Fulano', '01/01/2026 → 30/04/2026'],
 *       ]),
 *     ),
 *   },
 * ================================================================== */
const SECTIONS = {

  /* ---------- Informações da escola ---------- */
  inform: {
    label: 'Informações da escola',
    icon: '🏫',
    render: () => h.stack(
      h.header('Informações da escola', 'Acessos, credenciais e dados institucionais'),

      h.table('Menu Principal', [
        ['Educarweb',            h.link('Acessar', 'https://viamao.educarweb.net.br/Home')],
        ['Prefeitura de Viamão', h.link('Acessar', 'https://www.viamao.rs.gov.br/')],
        ['Site do Estado do RS', h.link('Acessar', 'https://www.estado.rs.gov.br/inicial')],
        ['Secretaria da Educação', h.link('Acessar', 'https://educacao.rs.gov.br/inicial')],
        ['Censo escolar',        h.link('Acessar', 'https://educacenso.inep.gov.br/educacenso/')],
        ['Sistema presença',     h.link('Acessar', 'https://presenca.mec.gov.br/seb/')],
        ['Parc Caed',            h.link('Acessar', 'https://parc.caeddigital.net')],
        ['Ionica',               h.link('Acessar', 'https://www.souionica.com.br/')],
        ['Sim Pontue',           h.link('Acessar', 'https://sim-publico.pontue.com.br/')],
        ['Ftd SIM',              h.link('Acessar', 'https://avaliacaoftdsim.com.br/')],
        ['1Doc',                 h.link('Acessar', 'https://viamao.1doc.com.br/?pg=painel/listar&l=1')],
        ['Gmail',                h.link('Acessar', 'https://mail.google.com/mail/u/1/?ogbl#inbox')],
        ['Google Drive',         h.link('Acessar', 'https://drive.google.com/drive/u/1/home')],
      ]),

      h.table('Geral', [
        ['Aviso',           'Todo dia 5, mandar nº de cópias da impressora do dionatan para SULCOP'],
        ['Anos iniciais',   '1º ao 5º ano'],
        ['Anos finais',     'Resto'],
        ['Email da escola', 'sargentoraymundo@edu.viamao.rs.gov.br'],
        ['INEP da escola',  '43167853'],
        ['Endereço',        'R. Padre Cacique, 265 - Vila Augusta, Viamão - RS, 94510-110'],
        ['CNPJ Prefeitura Viamão', '88000914000101'],
        ['CNPJ Caixa Escolar CPM', '01881699000109'],
        ['CEEE',            '2447677-3'],
      ]),

      h.table('Internet', [
        ['Modelo do roteador',    'G-1425GA'],
        ['Número do cliente Oi',  '2023833091'],
      ]),

      h.table('🔓 Senhas', [
        h.passRow('Senha email',
          'sargentoraymundo@edu.viamao.rs.gov.br:#Sargento2026#',
          [['Copiar email', 'sargentoraymundo@edu.viamao.rs.gov.br'],
           ['Copiar senha', '#Sargento2026#']]),

        h.passRow('Senha 1doc',
          'sargentoraymundo@edu.viamao.rs.gov.br:#Sargento2026#',
          [['Copiar email', 'sargentoraymundo@edu.viamao.rs.gov.br'],
           ['Copiar senha', '#Sargento2026#']]),

        h.passRow('Senha Ionica',
          'dionatan.santos@edu.viamao.rs.gov.br:@Diow0907',
          [['Copiar email', 'dionatan.santos@edu.viamao.rs.gov.br'],
           ['Copiar senha', '@Diow0907']]),

        h.passRow('Senha Parc Caed',
          '01679215094:01679215094',
          [['Copiar usuário', '01679215094'],
           ['Copiar senha', '01679215094']]),

        h.passRow('Senha FTD Sim',
          'sargentoraymundo@edu.viamao.rs.gov.br:09072011',
          [['Copiar email', 'sargentoraymundo@edu.viamao.rs.gov.br'],
           ['Copiar senha', '09072011']]),

        h.passRow('Matrícula das inscrições',
          '16785:2605',
          [['Copiar usuário', '16785'],
           ['Copiar senha', '2605']]),
      ]),

      h.table('Acessos', [
        ['Adm Sargento',      'jL588_@wRe'],
        ['Professores',       '@pedagogico23'],
        ['Ponto 1',           '11111987@#'],
        ['Ponto 2',           '@#11111987'],
        ['Ponto 4 biblioteca','BB11111987@'],
        ['Infantil',          'infantil2024'],
      ]),
    ),
  },

  /* ---------- Ferramentas ---------- */
  tools: {
    label: 'Ferramentas',
    icon: '🛠️',
    render: () => h.stack(
      h.header('Ferramentas', 'Conversão e manipulação de arquivos'),

      h.card(`
        <h3 class="form-title">🖼️ Images → PDF</h3>
        <form action="/convert_img_to_pdf" method="post" enctype="multipart/form-data" class="tool-form">
          <input type="file" name="images" accept="image/*" multiple required class="file-input">
          <button type="submit" class="btn btn-primary">Converter</button>
        </form>
      `),

      h.card(`
        <h3 class="form-title">📄 Merge PDFs</h3>
        <form action="/merge_pdfs" method="post" enctype="multipart/form-data" class="tool-form">
          <input type="file" name="pdfs" accept="application/pdf" multiple required class="file-input">
          <button type="submit" class="btn btn-primary">Juntar</button>
        </form>
      `),

      h.card(`
        <h3 class="form-title">🖼️ PDF → Image</h3>
        <form action="/convert_pdf_to_img" method="post" enctype="multipart/form-data" class="tool-form">
          <input type="file" name="pdf_file" accept="application/pdf" required class="file-input">
          <button type="submit" class="btn btn-primary">Converter</button>
        </form>
      `),
    ),
  },

  /* ---------- Matrícula ---------- */
  matricula: {
    label: 'Matrícula',
    icon: '📝',
    render: () => h.stack(
      h.header('Matrícula', 'Documentos necessários'),
      h.card(`
        ${h.list([
          'Identidade da mãe',
          'Identidade do aluno',
          'Comprovante de endereço',
          'Carteira de vacinação',
          'Designação de vaga',
          'Histórico da outra escola (se for transferência)',
        ])}
        <p style="margin-top:12px;font-size:13px;color:var(--muted)">
          <strong style="color:var(--neon)">Nota:</strong> Não é permitido recusar matrícula por falta de documento.
        </p>
      `),
    ),
  },

  /* ---------- Vagas ---------- */
  vagas: {
    label: 'Vagas',
    icon: '💺',
    render: () => h.stack(
      h.header('Vagas', 'Limites por turma e idades de corte'),
      h.table('Vagas permitidas por turma', [
        ['Jardim 1',   '20'],
        ['Jardim 2',   '25'],
        ['1º ano',     '25'],
        ['2º ao 4º',   '30'],
        ['5º ao 9º',   '35'],
      ]),
      h.table('Idades', [
        ['Jardim 1', '4 anos em 31/03/2026'],
        ['Jardim 2', '5 anos em 31/03/2026'],
        ['1º ano',   '6 anos em 31/03/2026'],
      ]),
    ),
  },

  /* ---------- Modelos 1doc ---------- */
  msgfrequentes: {
    label: 'Modelos 1doc',
    icon: '✉️',
    render: () => h.stack(
      h.header('Modelos 1doc', 'Mensagens frequentes'),
      h.card(`
        <h3 class="form-title">⚡ Conta de energia elétrica - Maio</h3>
        ${h.list([
          'Bom dia!',
          'Venho, por meio deste, encaminhar em anexo a conta de energia elétrica com data de vencimento 21/05/2026.',
          'SME - DG A/C Ana Gross',
        ])}
      `),
    ),
  },

  /* ---------- Regência ---------- */
  regencia: {
    label: 'Regência',
    icon: '👨‍🏫',
    render: () => h.stack(
      h.header('Regência', 'Encontrar todos os períodos em que o professor esteve na escola'),
      h.card(h.list([
        'Primeiro procurar se ele tem uma ficha antiga, se tiver tudo bem na ficha e mostrar a data de entrada e saída, usar.',
        'Se não tiver o dia exato, procurar na efetividade.',
        'Se ainda assim não achar, usar os livros-ponto para saber.',
      ])),
    ),
  },

  /* ---------- Censo escolar ---------- */
  censo: {
    label: 'Censo escolar',
    icon: '📊',
    render: () => h.stack(
      h.header('Censo escolar', 'Transferências, matrículas, alunos concluintes e reprovados'),
      h.card(h.list([
        'Se a data de corte é 28/05, verificar alunos transferidos e matrículas a partir de 29/05',
        'Quem entrou ou saiu a partir de 29/05, inclusive.',
      ])),
    ),
  },

  /* ---------- Sistema presença ---------- */
  presenca: {
    label: 'Sistema presença',
    icon: '📌',
    render: () => h.stack(
      h.header('Sistema presença', 'A cada 2 meses, ficar atento no site'),
      h.card(`
        <p style="margin-bottom:10px;font-size:13px;color:var(--muted)">
          Quando acabar o período e avisarem, começar relatório.
        </p>
        ${h.list([
          'Consulta estatística &gt; Estatística de frequência',
          'Sempre consultar as duas turmas, ver se trocou de turma',
          'Consulta estatística &gt; Estatística de frequência',
          'Ir em histórico &gt; pendentes, se tiver a recém vindo, colocar frequência da última escola. Se não mandaram a atual, repete a última. Se absolutamente não tiver da outra escola, azar do aluno, colocar frequência que tivermos.',
          'Conferir atestados médicos em justificar falta',
          'Verificar com a Margarete os motivos, pedir para ela preencher para cada aluno com baixa frequência',
          'Alunos nossos transferidos a pouco, transferir pra nova escola, mandar frequência e data de transferência pelo chat, pedir ajuda se necessário.',
        ])}
      `),
    ),
  },

  /* ---------- Uniformes ---------- */
  uniformes: {
    label: 'Uniformes',
    icon: '👕',
    render: () => h.stack(
      h.header('Uniformes'),
      h.card(`<p>O diretor solicitou uniformes com 1 tamanho maior para todos os alunos. Alunos vão provar os uniformes na escola. Se estiver errado, pedir a troca.</p>`),
    ),
  },

  /* ---------- Históricos ---------- */
  historicos: {
    label: 'Históricos',
    icon: '📜',
    render: () => h.stack(
      h.header('Históricos', 'Como emitir históricos'),
      h.card(h.list([
        'Marcar quase todas as opções no sistema, exceto assinaturas.',
        'Fazer 3 cópias: 2 para o responsável, 1 para a ficha do aluno.',
        'Podemos pedir 15 dias por lei se necessário.',
      ], true)),
    ),
  },

  /* ---------- Boletins ---------- */
  boletins: {
    label: 'Boletins',
    icon: '📋',
    render: () => h.stack(
      h.header('Boletins', 'Emissão de boletins'),
      h.card(`<p>Notas &gt; Emissão de boletim escolar. Lembre de separar anos iniciais e finais.</p>`),
    ),
  },

  /* ---------- Como entrar no 1Doc ---------- */
  onedoc: {
    label: 'Como entrar no 1Doc',
    icon: '🔐',
    render: () => h.stack(
      h.header('Como entrar no 1Doc'),
      h.card(`<p>Site do município → Processos → processos do servidor → Entrar com usuário e senha. Depois siga para protocolos.</p>`),
    ),
  },

  /* ---------- Nomes formatados / Python ---------- */
  nomes: {
    label: 'Nomes formatados / Python',
    icon: '🐍',
    render: () => h.stack(
      h.header('Nomes formatados', 'Como pegar nomes dos alunos formatados'),
      h.card(`
        <p>1. Consultas &gt; Estudantes por turma</p>
        <p>2. Imprimir como arquivo .html → Converter para texto</p>
        <p>3. Colocar o texto no seguinte programa em Python:</p>
        <div class="code-box"><pre><code>import re

# Padrão exemplo para extrair nomes (inclui acentos)
pattern = r'&lt;span style="font-family: Tahoma; color: #000000; font-size: 8px; line-height: 1\\.2070312;"&gt;([\\w\\sÁÉÍÓÚÀÈÌÒÙÂÊÎÔÛÃÕÇ]+)&lt;/span&gt;'
names = re.findall(pattern, html_string)
valid_names = [n for n in names if " " in n and not re.search(r'\\d', n)]
for name in valid_names: print(name)</code></pre></div>
      `),
    ),
  },

  /* ---------- Solicitação de passagem ---------- */
  passagem: {
    label: 'Solicitação de passagem',
    icon: '🚌',
    render: () => h.stack(
      h.header('Solicitação de passagem escolar'),
      h.card(`
        ${h.list([
          'Colocar na planilha de pedidos de passagem escolar.',
          'Entrar no site do município e abrir processo digital (1Doc).',
          'Enviar atestado de matrícula e frequência, documentos e números atualizados.',
          'Anotar número do protocolo e entregar ao responsável.',
        ], true)}
        <p style="margin-top:12px">
          <a href="https://docs.google.com/spreadsheets/d/1WdEMnS8YQ-Pv_mN5XGwtZLdukbT-3WmoLp_cfnItPkI/edit?usp=sharing"
             target="_blank" rel="noopener">📊 Planilha de exemplo ↗</a>
        </p>
      `),
    ),
  },

  /* ---------- Mapa da alimentação ---------- */
  merenda: {
    label: 'Mapa da alimentação',
    icon: '🍽️',
    render: () => h.stack(
      h.header('Mapa da alimentação'),
      h.card(`<p>1. Entrar no 1Doc → Novo → Documento. 2. Nomear como "Mapa Merenda Sargento Raymundo". 3. Classificar como estoque de alimentação. 4. Enviar para SME - Setor de alimentação.</p>`),
    ),
  },

  /* ---------- Suporte Educarweb ---------- */
  suporte: {
    label: 'Suporte Educarweb',
    icon: '🆘',
    render: () => h.stack(
      h.header('Suporte Educarweb'),
      h.card(`
        <p>Clicar no canto superior direito do Educarweb → Suporte.</p>
        <p style="margin-top:8px">Usuário padrão: <code class="cred">sargentoraymundo@edu.viamao.rs.gov.br:293949</code></p>
      `),
    ),
  },

  /* ================================================================
     EXEMPLOS de como adicionar seções futuras (deixe comentado):

     estagios: {
       label: 'Estágios probatórios',
       icon: '📅',
       render: () => h.stack(
         h.header('Estágios probatórios', 'Datas de estágio dos funcionários'),
         h.table('Funcionários', [
           ['Fulano',  '01/01/2026 → 30/04/2026'],
           ['Sicrano', '15/02/2026 → 15/06/2026'],
         ]),
       ),
     },
     ================================================================ */
};

/* ==================================================================
 * PLUGINS — seções carregadas dinamicamente de plugins/
 * ------------------------------------------------------------------
 * Cada plugin vira uma seção com prefixo "plugin_".
 * O form tem id="form_plugin_<id>" e o resultado é injetado em
 * "#plugin_result_<id>". Toda a fiação é feita pelo handler global
 * de submit (mais abaixo). Nada mais precisa ser feito aqui.
 * ================================================================== */
const PLUGINS_DATA = {{ plugins|tojson }};
for (const p of PLUGINS_DATA) {
  SECTIONS['plugin_' + p.id] = {
    label: p.label,
    icon: p.icon || '🔧',
    render: () => {
      const parts = [h.header(p.label, p.description || '')];
      if (p.info_html) parts.push(h.card(p.info_html));
      if (p.form_html) parts.push(h.card(p.form_html));
      parts.push(`<div id="plugin_result_${p.id}"></div>`);
      return h.stack(...parts);
    },
  };
}

/* ==================================================================
 * NAVEGAÇÃO — home ↔ seções
 * ================================================================== */
const homeView    = document.getElementById('homeView');
const contentView = document.getElementById('contentView');
const menuEl      = document.getElementById('menu');

let currentSection = 'home';

function markActive(id) {
  menuEl.querySelectorAll('a').forEach(a => {
    a.classList.toggle('active', a.dataset.section === id);
  });
}

function showHome() {
  currentSection = 'home';
  homeView.classList.remove('hidden');
  contentView.classList.add('hidden');
  contentView.innerHTML = '';
  markActive('home');
  const qi = document.getElementById('queryInput');
  if (qi) qi.focus();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function showSection(id) {
  const sec = SECTIONS[id];
  if (!sec) return;
  currentSection = id;
  homeView.classList.add('hidden');
  contentView.classList.remove('hidden');
  contentView.innerHTML = sec.render();
  markActive(id);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* Monta o menu: Home + todas as seções registradas */
(function buildMenu() {
  const homeLink = document.createElement('a');
  homeLink.href = 'javascript:void(0)';
  homeLink.dataset.section = 'home';
  homeLink.innerHTML = '<span class="menu-icon">🏠</span> Home — Buscar Aluno/Turma';
  homeLink.onclick = showHome;
  menuEl.appendChild(homeLink);

  Object.entries(SECTIONS).forEach(([id, sec]) => {
    const a = document.createElement('a');
    a.href = 'javascript:void(0)';
    a.dataset.section = id;
    a.innerHTML = `<span class="menu-icon">${sec.icon || '•'}</span> ${sec.label}`;
    a.onclick = () => showSection(id);
    menuEl.appendChild(a);
  });
})();

/* Estado inicial: home */
markActive('home');

/* ==================================================================
 * ANOTAÇÕES (localStorage)
 * ================================================================== */
function loadNotes() {
  const notesDiv = document.getElementById('notes');
  const notes = JSON.parse(localStorage.getItem('notes') || '[]');
  if (notes.length === 0) {
    notesDiv.innerHTML = '<p style="background:transparent;border:none;color:var(--muted);padding:0">Nenhuma anotação salva.</p>';
    return;
  }
  notesDiv.innerHTML = notes.slice().reverse().map(n => `<p>${escapeHtml(n)}</p>`).join('');
}

function saveNote(e) {
  e.preventDefault();
  const v = document.getElementById('note').value.trim();
  if (!v) return false;
  const agora = new Date();
  const dataFormatada = agora.toLocaleDateString('pt-BR');
  const horaFormatada = agora.toLocaleTimeString('pt-BR');
  const anotacaoComData = `${v} - ${dataFormatada} ${horaFormatada}`;
  const notes = JSON.parse(localStorage.getItem('notes') || '[]');
  notes.push(anotacaoComData);
  localStorage.setItem('notes', JSON.stringify(notes));
  document.getElementById('note').value = '';
  loadNotes();
  return false;
}

function clearNotes() {
  if (!confirm('Limpar todas as anotações?')) return;
  localStorage.removeItem('notes');
  loadNotes();
}

function escapeHtml(unsafe) {
  return unsafe.replace(/[&<"'>]/g, function(m) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#039;"}[m];
  });
}

loadNotes();

/* ==================================================================
 * BUSCA: Enter no campo → dispara botão "Buscar"
 * (preservando o comportamento original)
 * ================================================================== */
(function(){
  const form = document.getElementById('searchForm');
  form.addEventListener('keydown', function(e){
    if (e.key === 'Enter') {
      const ae = document.activeElement;
      if (ae && (ae.id === 'note' || ae.id === 'lagora-ai-input')) return;
      e.preventDefault();
      const buscarBtn = form.querySelector("input[type='submit'][value='Buscar']");
      if (buscarBtn) buscarBtn.click();
    }
  });
})();

/* Pequeno brilho no título ao focar o input (mantido do original) */
(function(){
  const qi = document.getElementById('queryInput');
  const title = document.querySelector('#homeView h2');
  if (!qi || !title) return;
  qi.addEventListener('focus', () => title.style.textShadow = '0 0 12px rgba(58,61,255,0.25)');
  qi.addEventListener('blur',  () => title.style.textShadow = 'none');
})();

/* ==================================================================
 * IA LAGORA — chat com o backend /lagora_ai
 * ================================================================== */
(function(){
  const sendBtn = document.getElementById('lagora-ai-send');
  const input   = document.getElementById('lagora-ai-input');
  const output  = document.getElementById('lagora-ai-output');
  if (!sendBtn || !input || !output) return;

  function addLine(text, isAI = false) {
    const div = document.createElement('div');
    div.textContent = text;
    if (isAI) {
      div.style.fontSize = '1.15em';
      div.style.color = '#000000';
      div.style.fontWeight = '600';
    }
    output.appendChild(div);
    output.scrollTop = output.scrollHeight;
    return div;
  }

  async function sendMessage() {
    const message = input.value.trim();
    if (!message) return;

    addLine('> ' + message);
    input.value = '';
    const thinking = addLine('Pensando 💫');

    try {
      const response = await fetch('/lagora_ai', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message })
      });
      const data = await response.json();
      thinking.remove();
      addLine(data.reply, true);
      console.log(data);
    } catch (err) {
      thinking.remove();
      addLine('connection error');
      console.error(err);
    }
  }

  sendBtn.addEventListener('click', sendMessage);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); sendMessage(); }
  });
})();

/* ==================================================================
 * COPIAR (delegação global — funciona em conteúdo dinâmico também)
 * ================================================================== */
document.addEventListener('click', async function(event) {
  const button = event.target.closest('.copy-btn');
  if (!button) return;

  const textToCopy = button.dataset.copy;
  const originalValue = button.dataset.defaultValue || button.value;

  if (!textToCopy) {
    button.value = 'Nada para copiar';
    setTimeout(() => { button.value = originalValue; }, 1400);
    return;
  }

  try {
    await navigator.clipboard.writeText(textToCopy);
    button.value = '✓ Copiado!';
    button.disabled = true;
    setTimeout(() => {
      button.value = originalValue;
      button.disabled = false;
    }, 1400);
  } catch (err) {
    button.value = 'Erro ao copiar';
    setTimeout(() => { button.value = originalValue; }, 1400);
    console.error('Erro ao copiar:', err);
  }
});

/* ==================================================================
 * PLUGINS — handler global de submit
 * ------------------------------------------------------------------
 * Qualquer <form id="form_plugin_<id>"> dispara POST para
 * /ferramentas/<id> e renderiza {html, download|error} em
 * #plugin_result_<id>. Funciona em qualquer seção (delegação global).
 * ================================================================== */
document.addEventListener('submit', async function(event) {
  const form = event.target;
  if (!form.id || !form.id.startsWith('form_plugin_')) return;
  event.preventDefault();

  const pluginId = form.id.substring('form_plugin_'.length);
  const resultEl = document.getElementById('plugin_result_' + pluginId);
  if (!resultEl) return;

  resultEl.innerHTML = '<div class="card"><p style="color:var(--muted)">⏳ Processando…</p></div>';

  try {
    const res = await fetch('/ferramentas/' + pluginId, {
      method: 'POST',
      body: new FormData(form),
    });
    const data = await res.json();

    if (data.error) {
      resultEl.innerHTML =
        `<div class="card"><p style="color:var(--danger);font-weight:600">❌ ${h.esc(data.error)}</p></div>`;
      return;
    }

    resultEl.innerHTML = '';

    if (data.html) {
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = data.html;
      resultEl.appendChild(card);
    }

    if (data.download && data.download.content) {
      let blob;
      const dl = data.download;
      if (dl.encoding === 'base64') {
        const bin = atob(dl.content);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        blob = new Blob([bytes], { type: dl.mimetype || 'application/octet-stream' });
      } else {
        blob = new Blob([dl.content], { type: dl.mimetype || 'text/plain;charset=utf-8' });
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = dl.filename || 'resultado.txt';
      a.className = 'btn btn-primary';
      a.style.display = 'inline-block';
      a.style.marginTop = '12px';
      a.textContent = '⬇️ ' + (dl.filename || 'Baixar resultado');
      resultEl.appendChild(a);
    }
  } catch (err) {
    resultEl.innerHTML =
      `<div class="card"><p style="color:var(--danger)">❌ ${h.esc(err.message || err)}</p></div>`;
  }
});
</script>
</body>
</html>
"""

# =========================================================================
# PARSING (inalterado)
# =========================================================================
number_database = open(os.path.join("static", "phones.txt"), "r", encoding='utf-8').readlines()

def generate_vcf(student):
    vcards = []
    phones = [p.strip() for p in student.phones.split(",") if p.strip()]
    for phone in phones:
        parts = phone.split()
        main = " ".join(parts[:2])
        extra = " ".join(parts[2:]) if len(parts) > 2 else ""
        name = student.name
        if extra != "":
            name = extra + " | " + student.name
        vcards.append(f"""BEGIN:VCARD
VERSION:3.0
FN:{name}
TEL:{main}
END:VCARD""")
    return "\n".join(vcards)

def normalize_string(input_string):
    normalized = unicodedata.normalize('NFD', input_string)
    return ''.join([c for c in normalized if unicodedata.category(c) != 'Mn']).lower()

def normalize_text(text):
    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return text

def calculate_age(birth_str):
    birth_date = datetime.strptime(birth_str, "%d/%m/%Y")
    today = datetime.today()
    age = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        age -= 1
    return age

class Student:
    def __init__(self, ra, name, sex, birth, cohort, state, mat, phones):
        self.ra = ra
        self.name = name
        self.sex = sex
        self.birth = birth
        self.cohort = cohort
        self.state = state
        self.mat = mat
        self.phones = phones
        self.age = calculate_age(self.birth)

current_dir = os.path.dirname(os.path.abspath(__file__))
html_landmark = '<span style="font-family: \'DejaVu Sans\', Arial, Helvetica, sans-serif; color: #000000; font-size: 8px; line-height: 1.1640625;">'
html_landmark2 = '<span style="font-family: \'DejaVu Sans\', Arial, Helvetica, sans-serif; color: #000000; font-size: 8px; line-height: 1; *line-height: normal;">'
html_files = [f for f in os.listdir(current_dir) if f.lower().endswith('.html')]
file = open(os.path.join(current_dir, html_files[0]), "r", encoding="utf-8").readlines()

students, data, seen_ra = [], [], []
current_cohort = ""
for line in file:
    if "TURMA " in line or "Turma: ":
        if "TURMA " in line:
            current_cohort = line.replace('<span style="font-family: \'DejaVu Sans\', Arial, Helvetica, sans-serif; color: #000000; font-size: 10px; line-height: 1.1640625; font-weight: bold;">Turma:', "").replace(" (EMEF SARGENTO MANOEL RAYMUNDO SOARES)</span></td>", "")
        if "Turma: J" in line:
            current_cohort = line.replace('<span style="font-family: \'DejaVu Sans\', Arial, Helvetica, sans-serif; color: #000000; font-size: 10px; line-height: 1.1640625; font-weight: bold;">Turma:', "").replace(" (EMEF SARGENTO MANOEL RAYMUNDO SOARES)</span></td>", "").replace(" &nbsp;(EMEF SARGENTO MANOEL RAYMUNDO SOARES)</span></td>", "")

    if html_landmark in line or html_landmark2 in line:
        data.append(line.replace(html_landmark, "").replace(html_landmark2, "").replace("</span></td>", "").replace("<br/>", ", ").strip())
        if len(data) == 9:
            print(data)
            if data[0] not in seen_ra and "AEE" not in str(data[5]):
                phones_list = data[8].replace(" ", "").split(",")
                newphonelist = []
                for number in phones_list:
                    if ")" in number:
                        raw_number = number.split(")")[1]
                        phone_recipient = ""
                        for db_line in number_database:
                            if raw_number in db_line:
                                phone_recipient = db_line.split("=")[1]
                        newphonelist.append(number.replace(")", ") ") + " " + phone_recipient)
                identified_phones = ", ".join(newphonelist)
                cohort = data[5].replace("TURMA ", "").replace("&nbsp;", "").strip()
                students.append(Student(data[0], data[1].replace("&nbsp;", ""), data[2], data[3],
                                        cohort, data[6], data[7], identified_phones))
                seen_ra.append(data[0])
            else:
                for s in students:
                    if data[0] == s.ra and s.phones != data[8]:
                        s.phones += ", " + data[8]
            data = []

write_data_file(text_to_mistral_jsonl(students_to_string(students)))

with open("alunos.txt", "w", encoding="utf-8") as f:
    for aluno in students:
        nome = normalize_text(aluno.name.lower()).strip()
        turma = aluno.cohort.replace("TURMA ", "").strip()
        f.write(f"{nome}|{turma}\n")

print(f"Arquivo gerado com {len(students)} alunos.")

# =========================================================================
# ROTAS
# =========================================================================
@app.route("/lagora_ai", methods=["POST"])
def lagora_ai():
    try:
        print("AI ROUTE HIT")
        data = request.get_json()
        message = data.get("message", "")
        print("USER:", message)
        response = askmistral(message)
        reply = str(response.choices[0].message.content).replace("*", "")
        print("AI:", reply)
        return jsonify({"reply": reply})
    except Exception as e:
        print("AI ERROR:", e)
        return jsonify({"reply": "Deixa eu descansar um pouco 😵"})

@app.route("/", methods=["GET"])
def index():
    query = request.args.get("query", "").strip()
    action = request.args.get("action")
    results = []
    mode = None

    if query and action == "Buscar":
        mode = "search"
        map1 = ["JARDIM I A", "JARDIM II B", "JARDIM II A", "JARDIM I B"]
        map2 = ["J1A", "J2B", "J2A", "J1B"]
        if query in map2:
            query = map1[map2.index(query)]
        for s in students:
            if query.lower() in s.cohort.lower() or normalize_string(query) in normalize_string(s.name):
                results.append(s)

    elif query and action == "Exportar dados da turma":
        mode = "export"
        RAs, fullnames, firstnames, lastnames, users, passwords, cohorts, emails, series = (
            ["RA: "], ["Nomes completos: "], ["Primeiros nomes: "], ["Sobrenomes: "],
            ["Usuários"], ["Senhas: "], ["Turmas: "], ["Emails: "], ["Série/ano"]
        )
        for s in students:
            if query.lower() in s.cohort.lower():
                RAs.append(s.ra)
                fullnames.append(str(s.name))
                firstnames.append(s.name.split()[0])
                lastnames.append(" ".join(s.name.split()[1:]))
                users.append(normalize_text(s.name.lower()).replace(" ", ""))
                passwords.append("sargento2025")
                cohorts.append(str(s.cohort).replace("TURMA ", ""))
                emails.append(normalize_text(s.name.lower()).replace(" ", "") + "@gmail.com")
                series.append(str(s.cohort).replace("TURMA ", "")[1] + "º ano do Ensino Fundamental")
        results = [
            "Número de alunos: " + str(len(RAs) - 1),
            "<br>".join(RAs),
            "<br>".join(fullnames),
            "<br>".join(firstnames),
            "<br>".join(lastnames),
            "<br>".join(emails),
            "<br>".join(passwords),
            "<br>".join(series),
            "<br>".join(cohorts),
            "<br>".join(users),
        ]

    elif action == "Exportar arquivo de contatos":
        vcards = [generate_vcf(s) for s in students]
        return Response(
            "\n\n".join(vcards),
            mimetype="text/vcard",
            headers={"Content-Disposition": 'attachment; filename="contatos.vcf"'}
        )

    elif query and action == "Procurar nas atas 2021 - 1988":
        mode = "atas"
        pdf_folder = os.path.join("static", "atas_organizadas")
        search_hits = search_pdfs(pdf_folder, query)
        results = []
        for year, page, path in search_hits:
            link = url_for("download_pdf", filepath=path)
            results.append(f"Ano: {year} | Página: {page} — <a href='{link}'>Baixar PDF</a>")

    # metadata dos plugins (sem as funções handle — só o que o HTML precisa)
    plugins_meta = [
        {
            'id': p['id'],
            'label': p['label'],
            'icon': p['icon'],
            'description': p['description'],
            'form_html': p['form_html'],
            'info_html': p['info_html'],
        }
        for p in PLUGINS.values()
    ]

    return render_template_string(
        HTML_TEMPLATE,
        results=results,
        url_for=url_for,
        mode=mode,
        plugins=plugins_meta,
    )

@app.route("/download_pdf")
def download_pdf():
    filepath = request.args.get("filepath")
    return send_file(filepath, as_attachment=True)

@app.route("/tools")
def tools_home():
    return render_template_string("<h1>Document Tools</h1> ...form HTML here...")

# =========================================================================
# ROTA DOS PLUGINS — executa handle(request) de plugins/
# =========================================================================
@app.route("/ferramentas/<plugin_id>", methods=["POST"])
def run_plugin(plugin_id):
    plugin = PLUGINS.get(plugin_id)
    if not plugin:
        return jsonify({"error": f"Plugin '{plugin_id}' não encontrado."}), 404
    if not plugin.get('handle'):
        return jsonify({"error": f"Plugin '{plugin_id}' não tem função handle()."}), 500
    try:
        result = plugin['handle'](request)
        if not isinstance(result, dict):
            return jsonify({"error": "handle() deve retornar um dict."}), 500
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Erro no plugin '{plugin_id}': {e}"}), 500

@app.route("/convert_pdf_to_img", methods=["POST"])
def convert_pdf_to_img():
    file = request.files["pdf_file"]
    fmt = request.form.get("format", "png")
    tmpdir = tempfile.mkdtemp()
    pdf_path = os.path.join(tmpdir, file.filename)
    file.save(pdf_path)
    images = convert_from_path(pdf_path)
    output_files = []
    for i, img in enumerate(images):
        out_path = os.path.join(tmpdir, f"page_{i+1}.{fmt}")
        img.save(out_path, fmt.upper())
        img.close()
        output_files.append(out_path)
    if len(output_files) == 1:
        return send_file(output_files[0], as_attachment=True)
    zip_path = os.path.join(tmpdir, "images.zip")
    with zipfile.ZipFile(zip_path, "w") as zipf:
        for f in output_files:
            zipf.write(f, os.path.basename(f))
    return send_file(zip_path, as_attachment=True, download_name="images.zip")

@app.route("/convert_img_to_pdf", methods=["POST"])
def convert_img_to_pdf():
    files = request.files.getlist("images")
    tmpdir = tempfile.mkdtemp()
    output_path = os.path.join(tmpdir, "merged.pdf")
    img_paths = []
    for file in files:
        img_tmp = os.path.join(tmpdir, file.filename)
        file.save(img_tmp)
        img_paths.append(img_tmp)
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(img_paths))
    return send_file(output_path, as_attachment=True)

@app.route("/merge_pdfs", methods=["POST"])
def merge_pdfs():
    files = request.files.getlist("pdfs")
    sorted_files = sorted(files, key=lambda f: f.filename.lower())
    merger = PdfMerger()
    for file in sorted_files:
        merger.append(file)
    output_path = os.path.join(tempfile.gettempdir(), "merged.pdf")
    merger.write(output_path)
    merger.close()
    return send_file(output_path, as_attachment=True)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

if __name__ == "__main__":
    port = 5000
    ip = get_local_ip()
    print(f"Servidor rodando em: http://{ip}:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)