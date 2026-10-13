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

        const img = document.createElement("img");
        img.style.cssText = "width:100%;height:100%;object-fit:contain;";
        node.addDOMWidget("preview", "preview", img, { serialize: false, getMinHeight: () => 150 });

        const update = () => {
            const v = (data.value || "").trim();
            img.style.visibility = v ? "visible" : "hidden";
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
