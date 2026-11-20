import { app } from "../../scripts/app.js";

// Live preview of the base64 in "Load Image (Base64)": renders whatever the
// textarea holds, client-side only — nothing is executed, sent or saved.
app.registerExtension({
    name: "nakagawa.LoadImageBase64Preview",
    nodeCreated(node) {
        if (node.comfyClass !== "LoadImageBase64Nak") return;

        const data = node.widgets.find((w) => w.name === "data");
        // Native file picker: the chosen image fills the textarea as a data URI
        const file = document.createElement("input");
        file.type = "file";
        file.accept = "image/*";
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
        node.addWidget("button", "load image as base64", null, () => file.click(), { serialize: false });

        // Same layout as the native Load Image preview: fixed 220px area, image
        // contained in it, dimensions label below
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
        node.addDOMWidget("preview", "preview", box, { serialize: false, getMinHeight: () => 220 });

        // The multiline textarea forces a 400px min width; size the node like
        // the native one instead (computeSize + the frontend's default 60px pad)
        const s = node.computeSize();
        node.setSize([s[0] + 60, s[1]]);

        const update = () => {
            const v = (data.value || "").trim();
            box.style.visibility = v ? "visible" : "hidden";
            if (!v) dims.textContent = "";
            // Raw base64 gets a generic prefix; the browser sniffs the real format (png/jpeg/webp)
            if (v) img.src = v.startsWith("data:") ? v : `data:image/png;base64,${v}`;
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
