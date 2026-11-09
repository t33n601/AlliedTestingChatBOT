import base64
import os
from pathlib import Path

import gradio as gr
import requests

API_URL = os.getenv("KB_API_URL", "http://localhost:8091/kb/answer")
STATS_URL = API_URL.rsplit("/", 1)[0] + "/stats"
UI_HOST = os.getenv("KB_UI_HOST", "127.0.0.1")
UI_PORT = int(os.getenv("KB_UI_PORT", "7871"))

UI_DIR = Path(__file__).parent / "ui"
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link href="https://fonts.googleapis.com/css2?family=Silkscreen:wght@400;700'
         '&family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600;700'
         '&display=swap" rel="stylesheet">')

EXAMPLES = [
    "What services do you offer?",
    "Tell me about your QA consulting",
    "How do you handle security testing?",
    "What products does the company have?",
]


# ---------------------------------------------------------------- chat logic

def flatten_history(history):
    """Gradio history -> flat list [user, bot, user, bot, ...].
    Works with both formats: (user, bot) pairs and {"role", "content"} dicts."""
    flat = []
    for item in history:
        if isinstance(item, dict):
            flat.append(str(item.get("content", "")))
        else:
            user_msg, bot_msg = item
            flat.append(user_msg)
            flat.append(bot_msg or "")
    return flat


def strip_thinking(text):
    """qwen3 wraps its reasoning in <think>...</think> -- keep only the answer."""
    parts = text.split("</think>")
    return (parts[1] if len(parts) > 1 else parts[0]).strip()


def reply(message, history):
    """
    message: last message from the user
    history: previous turns of the conversation
    """
    all_messages = flatten_history(history) + [message]
    try:
        response = requests.post(API_URL, json=all_messages, timeout=300)
        response.raise_for_status()
        data = response.json()
        print(f"response {data}")  # DEBUG
        return strip_thinking(data["answer"])
    except requests.exceptions.RequestException as e:
        return f"Error connecting to API: {e}"
    except KeyError:
        return "Error: unexpected answer from server"
    except Exception as e:
        return f"Unknown error: {e}"


# ---------------------------------------------------------------- decoration

def logo_data_uri():
    """Allied Testing logo, inlined so Gradio needs no static route."""
    raw = (UI_DIR / "logo.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(raw).decode()


from ui.motif import motif_svg  # noqa: E402


def fetch_stats():
    try:
        return requests.get(STATS_URL, timeout=5).json()
    except Exception:
        return None


def hero_html():
    stats = fetch_stats()
    if stats:
        tiles = [(stats["documents"], "documents"), (stats["chunks"], "chunks"),
                 (768, "dim vectors")]
    else:
        tiles = [("—", "documents"), ("—", "chunks"), ("—", "API offline")]
    tiles_html = "".join(f'<div class="kb-stat"><b>{v}</b><small>{k}</small></div>' for v, k in tiles)
    return f"""
    <div class="kb-hero">
      <p class="kb-kicker">&gt; knowledge assistant · RAG_</p>
      <h1 class="kb-title">QA + Testing<br>answers from<br><em>the source</em></h1>
      <p class="kb-lead">Ask anything about Allied Testing's services, products and expertise.
        Every answer is retrieved from the knowledge base by meaning, then written by a local
        LLM — no cloud, no guessing.</p>
      <div class="kb-stats">{tiles_html}</div>
      <div class="kb-pipeline">
        <span>Retrieve</span><i>▶</i><span>Augment</span><i>▶</i><span>Generate</span>
      </div>
      <div class="kb-motif">{motif_svg()}</div>
    </div>
    """


def chat_head_html():
    stats = fetch_stats()
    status = (f'<span class="kb-live">{stats["chat_model"]} · online</span>' if stats
              else '<span class="kb-live off">API offline</span>')
    return f'<div class="kb-chat-head"><h3>KB Helper</h3>{status}</div>'


TOPBAR = f"""
<div class="kb-bar">
  <div class="kb-logo"><img src="{logo_data_uri()}" alt="Allied Testing"></div>
  <div class="kb-nav"><span class="on">Assistant</span><span>Services</span>
    <span>Products</span><span>Knowledge</span></div>
</div>
"""

PLACEHOLDER = """
<div class="kb-empty">
  <div class="kb-mini-mark"><span>1011</span><span>0110</span></div>
  <h4>How can I help?</h4>
  <p>Pick a question below or type your own</p>
</div>
"""

FOOT = """
<div class="kb-foot"><span>KB Helper · local RAG over Allied Testing knowledge</span>
<span>Postgres + pgvector · nomic-embed-text · qwen3</span></div>
"""

theme = gr.themes.Base(
    primary_hue=gr.themes.colors.red,
    neutral_hue=gr.themes.colors.zinc,
    font=[gr.themes.GoogleFont("IBM Plex Sans"), "system-ui", "sans-serif"],
).set(
    body_background_fill="#2f2f31", body_background_fill_dark="#2f2f31",
    body_text_color="#ececea", body_text_color_dark="#ececea",
    background_fill_primary="transparent", background_fill_primary_dark="transparent",
    background_fill_secondary="transparent", background_fill_secondary_dark="transparent",
    block_background_fill="transparent", block_background_fill_dark="transparent",
    block_border_width="0px", block_shadow="none", block_shadow_dark="none",
    border_color_primary="rgba(151,150,136,0.35)", border_color_primary_dark="rgba(151,150,136,0.35)",
    input_background_fill="transparent", input_background_fill_dark="transparent",
    button_primary_background_fill="#c14031", button_primary_background_fill_dark="#c14031",
    button_primary_text_color="#ffffff", button_primary_text_color_dark="#ffffff",
    color_accent_soft="rgba(193,64,49,0.2)", color_accent_soft_dark="rgba(193,64,49,0.2)",
)

with gr.Blocks(title="KB Helper", theme=theme, css_paths=[UI_DIR / "style.css"],
               head=FONTS, fill_width=True) as demo:
    gr.HTML(TOPBAR, elem_id="kb-topbar")
    with gr.Row(equal_height=False):
        with gr.Column(scale=5, min_width=320, elem_id="kb-hero"):
            hero = gr.HTML(hero_html)
        with gr.Column(scale=6, min_width=340, elem_id="kb-chat-col"):
            head = gr.HTML(chat_head_html)
            gr.ChatInterface(
                fn=reply,
                type="messages",
                chatbot=gr.Chatbot(
                    elem_id="kb-chat", type="messages", height=520,
                    placeholder=PLACEHOLDER, show_label=False,
                    avatar_images=(None, str(UI_DIR / "bot.svg")),
                ),
                textbox=gr.Textbox(placeholder="Ask about services, products, expertise…",
                                   container=False, elem_id="kb-input", scale=7,
                                   submit_btn="Ask"),
                examples=EXAMPLES,
                run_examples_on_click=True,
            )
    gr.HTML(FOOT)
    # refresh live numbers on every page load
    demo.load(hero_html, outputs=hero)
    demo.load(chat_head_html, outputs=head)

if __name__ == "__main__":
    demo.launch(server_name=UI_HOST, server_port=UI_PORT, share=False,
                favicon_path=str(UI_DIR / "favicon-64.png"))
