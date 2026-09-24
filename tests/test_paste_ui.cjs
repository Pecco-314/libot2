// Exercise clipboard/drop events and sequential uploads without a browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor() { this.children = []; this.value = ''; this.files = []; }
    append(...items) { this.children.push(...items); }
    replaceChildren() { this.children = []; }
}
const elements = Object.fromEntries(['upload', 'file-picker', 'queue', 'upload-status', 'upload-progress', 'save', 'choose-files'].map(id => [id, new Element()]));
const note = new Element();
const csrf = {value: 'token'};
elements.upload.action = 'http://example.test:5000/paste/';
elements.upload.querySelector = selector => selector === 'textarea' ? note : csrf;
const events = {};
const windowEvents = {};
const requests = [];
let navigated = false;
const failures = [];
const context = {
    document: {
        getElementById: id => elements[id], createElement: () => new Element(),
        addEventListener: (name, callback) => events[name] = callback,
        body: {classList: {add() {}, remove() {}}},
    },
    window: {
        addEventListener: (name, callback) => windowEvents[name] = callback,
        location: {assign() { navigated = true; }},
    },
    URL: {createObjectURL: () => 'blob:test', revokeObjectURL() {}},
    FormData: class { constructor() { this.fields = {}; } append(k, v) { this.fields[k] = v; } },
    XMLHttpRequest: class {
        constructor() { this.upload = {}; }
        open() {} setRequestHeader() {}
        send(data) {
            requests.push(data.fields);
            this.status = failures.shift() || 201;
            this.response = this.status === 201 ? {id: 'saved'} : null;
            this.onload();
        }
    },
};
vm.runInNewContext(fs.readFileSync('src/web/static/paste.js', 'utf8'), context);
const image = {name: 'screen.png', size: 123, type: 'image/png'};
let prevented = false;
events.paste({clipboardData: {items: [{kind: 'string'}]}, preventDefault() { prevented = true; }});
assert.equal(prevented, false, 'plain text paste remains native');
events.paste({clipboardData: {items: [{kind: 'file', getAsFile: () => image}]}, preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(elements.queue.children.length, 1);
assert.equal(elements.queue.children[0].children[0].src, 'blob:test');
events.drop({dataTransfer: {types: ['Files'], files: [image]}, preventDefault() {}});
assert.equal(elements.queue.children.length, 2);
events.drop({dataTransfer: {types: ['Files'], files: [{...image, size: 101 * 1024 * 1024}]}, preventDefault() {}});
assert.equal(elements.queue.children.length, 2, 'oversized files are rejected');
note.value = 'caption';
(async () => {
    failures.push(201, 500);
    await elements.upload.onsubmit({preventDefault() {}});
    assert.equal(requests.length, 2);
    assert.equal(requests[0].text, 'caption');
    assert.equal(requests[1].text, '');
    assert.equal(elements.queue.children.length, 1, 'failed file remains queued');
    assert.equal(navigated, false);
    assert.equal(elements.save.disabled, false);
    await elements.upload.onsubmit({preventDefault() {}});
    assert.equal(requests.length, 3);
    assert.equal(elements.queue.children.length, 0);
    assert.equal(navigated, true);
    let blocked = false;
    windowEvents.beforeunload({preventDefault() { blocked = true; }});
    assert.equal(blocked, false, 'successful navigation has no unsaved warning');
    console.log('Paste UI: text paste, image paste, drop, limits, preview, partial failure and retry passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
