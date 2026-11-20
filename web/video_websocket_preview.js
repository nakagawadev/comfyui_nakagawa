import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Plays the video the websocket nodes send (binary event 100) inside the node,
// straight from the websocket bytes: nothing is written to disk, and the
// preview is gone on page reload (like SaveImageWebsocket's).
const NODES = ["SaveVideoWebsocket", "SaveWEBMWebsocket"];
const VIDEO_EVENT = 100;
const MIME = { mp4: "video/mp4", webm: "video/webm", mov: "video/quicktime", mkv: "video/x-matroska" };

let executingId = null;
let boundSocket = null;

// Payload after ComfyUI's 4-byte event type: "VIDF" | uint32 LE format length | format | video
function parse(buffer) {
    const view = new DataView(buffer);
    if (view.getUint32(0) !== VIDEO_EVENT) return null;
    if (new TextDecoder().decode(buffer.slice(4, 8)) !== "VIDF") return null;
    const len = view.getUint32(8, true);
    const format = new TextDecoder().decode(buffer.slice(12, 12 + len));
    return { format, data: buffer.slice(12 + len) };
}

function onMessage(event) {
    if (!(event.data instanceof ArrayBuffer)) return;
    const msg = parse(event.data);
    // ponytail: root-graph nodes only; a node inside a subgraph gets an execution id like "5:3"
    const node = msg && executingId != null && app.graph.getNodeById(executingId);
    if (!node?.showWsVideo) return;
    node.showWsVideo(new Blob([msg.data], { type: MIME[msg.format] ?? `video/${msg.format}` }));
}

app.registerExtension({
    name: "nakagawa.VideoWebsocketPreview",
    setup() {
        // The frontend replaces api.socket on reconnect; rebinding on every
        // "executing" covers that, since our message always follows one.
        api.addEventListener("executing", ({ detail }) => {
            executingId = detail;
            if (api.socket && api.socket !== boundSocket) {
                api.socket.addEventListener("message", onMessage);
                boundSocket = api.socket;
            }
        });
    },
    nodeCreated(node) {
        if (!NODES.includes(node.comfyClass)) return;

        // Same layout as the Load Image (Base64) preview: fixed 220px area, contained
        const frame = document.createElement("div");
        frame.style.cssText = "position:relative;min-height:220px;visibility:hidden;";
        const video = document.createElement("video");
        video.style.cssText = "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;";
        Object.assign(video, { controls: true, loop: true, muted: true, autoplay: true, playsInline: true });
        frame.append(video);
        node.addDOMWidget("preview", "preview", frame, { serialize: false, getMinHeight: () => 220 });

        node.showWsVideo = (blob) => {
            if (video.src) URL.revokeObjectURL(video.src);
            video.src = URL.createObjectURL(blob);
            frame.style.visibility = "visible";
        };
        const onRemoved = node.onRemoved;
        node.onRemoved = function () {
            if (video.src) URL.revokeObjectURL(video.src);
            return onRemoved?.apply(this, arguments);
        };
    },
});
