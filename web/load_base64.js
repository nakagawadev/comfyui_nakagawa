import { app } from "../../scripts/app.js";

// Live preview of the base64 in the "Load ... (Base64)" nodes: renders whatever
// the textarea holds, client-side only — nothing is executed, sent or saved.

// Image: same layout as the native Load Image preview, a fixed 220px area with
// the image contained in it and its dimensions below
function imagePreview() {
    const box = document.createElement("div");
    box.style.cssText = "display:flex;flex-direction:column;align-items:center;min-height:220px;";
    // Absolutely positioned so the image's natural size never stretches the node
    const frame = document.createElement("div");
    frame.style.cssText = "position:relative;flex:1;min-height:0;width:100%;";
    const img = document.createElement("img");
    img.style.cssText = "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;";
    frame.append(img);
    const dims = document.createElement("div");
    dims.style.cssText = "font-size:12px;padding:4px 0;";
    img.onload = () => (dims.textContent = `${img.naturalWidth} × ${img.naturalHeight}`);
    box.append(frame, dims);
    return {
        el: box,
        height: 220,
        set(src) {
            if (!src) dims.textContent = "";
            else img.src = src;
        },
    };
}

function audioPreview() {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.style.cssText = "width:100%;";
    return { el: audio, height: 60, set: (src) => src && (audio.src = src) };
}

// Raw base64 gets a generic prefix; the browser sniffs the real format
const KINDS = {
    LoadImageBase64Nak: { kind: "image", mime: "image/png", preview: imagePreview },
    LoadAudioBase64Nak: { kind: "audio", mime: "audio/mpeg", preview: audioPreview },
};

app.registerExtension({
    name: "nakagawa.LoadBase64Preview",
    nodeCreated(node) {
        const cfg = KINDS[node.comfyClass];
        if (!cfg) return;

        const data = node.widgets.find((w) => w.name === "data");
        // Native file picker: the chosen file fills the textarea as a data URI
        const file = document.createElement("input");
        file.type = "file";
        file.accept = `${cfg.kind}/*`;
        file.onchange = () => {
            const f = file.files[0];
            if (!f) return;
            const reader = new FileReader();
            reader.onload = () => {
                data.value = reader.result;
                update();
                node.setDirtyCanvas(true, true);
            };
            reader.readAsDataURL(f);
            file.value = "";
        };
        node.addWidget("button", `load ${cfg.kind} as base64`, null, () => file.click(), { serialize: false });

        const preview = cfg.preview();
        node.addDOMWidget("preview", "preview", preview.el, { serialize: false, getMinHeight: () => preview.height });

        // The multiline textarea forces a 400px min width; size the node like
        // the native one instead (computeSize + the frontend's default 60px pad)
        const s = node.computeSize();
        node.setSize([s[0] + 60, s[1]]);

        const update = () => {
            const v = (data.value || "").trim();
            preview.el.style.visibility = v ? "visible" : "hidden";
            preview.set(v && (v.startsWith("data:") ? v : `data:${cfg.mime};base64,${v}`));
        };

        // Typing/pasting in the textarea
        data.inputEl?.addEventListener("input", update);
        // Programmatic changes
        const callback = data.callback;
        data.callback = function () {
            update();
            return callback?.apply(this, arguments);
        };
        // Loading a workflow that already has the base64
        const onConfigure = node.onConfigure;
        node.onConfigure = function () {
            const r = onConfigure?.apply(this, arguments);
            update();
            return r;
        };

        update();
    },
});
