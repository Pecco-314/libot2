(() => {
    'use strict';
    const form = document.getElementById('upload');
    const picker = document.getElementById('file-picker');
    const queue = document.getElementById('queue');
    const status = document.getElementById('upload-status');
    const progress = document.getElementById('upload-progress');
    const save = document.getElementById('save');
    const text = form.querySelector('textarea');
    const choose = document.getElementById('choose-files');
    let pending = [];
    let busy = false;
    let dragDepth = 0;

    function render() {
        queue.replaceChildren();
        for (const entry of pending) {
            const card = document.createElement('div');
            card.className = 'queued';
            if (entry.url) {
                const img = document.createElement('img');
                img.src = entry.url;
                img.alt = entry.file.name;
                card.append(img);
            }
            const label = document.createElement('p');
            label.textContent = `${entry.file.name} · ${(entry.file.size / 1024 / 1024).toFixed(2)} MiB`;
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.textContent = '移除';
            remove.disabled = busy;
            remove.onclick = () => {
                if (entry.url) URL.revokeObjectURL(entry.url);
                pending = pending.filter(item => item !== entry);
                render();
            };
            card.append(label, remove);
            queue.append(card);
        }
        save.textContent = busy ? '正在上传…' : pending.length ? `保存 ${pending.length} 个文件` : '保存文字';
    }

    function addFiles(files) {
        if (busy) {
            status.textContent = '请等当前上传完成后再添加图片。';
            return;
        }
        const errors = [];
        for (const file of files) {
            if (file.size > 100 * 1024 * 1024) {
                errors.push(`${file.name} 超过 100 MiB，未加入。`);
                continue;
            }
            const previewable = /^image\/(png|jpeg|gif|webp|avif|bmp)$/.test(file.type);
            pending.push({file, url: previewable ? URL.createObjectURL(file) : null});
        }
        status.textContent = errors.join('\n');
        render();
    }

    choose.onclick = () => picker.click();
    picker.onchange = () => { addFiles(picker.files); picker.value = ''; };
    document.addEventListener('paste', event => {
        const files = Array.from(event.clipboardData?.items || [])
            .filter(item => item.kind === 'file').map(item => item.getAsFile()).filter(Boolean);
        if (!files.length) return; // Keep normal text paste, including inside the textarea.
        event.preventDefault();
        addFiles(files);
    });
    const hasFiles = event => Array.from(event.dataTransfer?.types || []).includes('Files');
    document.addEventListener('dragenter', event => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        dragDepth++;
        document.body.classList.add('dragging');
    });
    document.addEventListener('dragover', event => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = busy ? 'none' : 'copy';
    });
    document.addEventListener('dragleave', event => {
        if (!hasFiles(event)) return;
        dragDepth = Math.max(0, dragDepth - 1);
        if (!dragDepth) document.body.classList.remove('dragging');
    });
    document.addEventListener('drop', event => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        dragDepth = 0;
        document.body.classList.remove('dragging');
        addFiles(event.dataTransfer.files);
    });

    function upload(data, number, total) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', form.action);
            xhr.setRequestHeader('Accept', 'application/json');
            xhr.responseType = 'json';
            xhr.timeout = 30 * 60 * 1000;
            xhr.upload.onprogress = event => {
                const percent = event.lengthComputable ? Math.round(event.loaded / event.total * 100) : 0;
                progress.value = percent;
                status.textContent = `正在保存 ${number}/${total} · ${percent}%${percent === 100 ? '，服务器处理中…' : ''}`;
            };
            xhr.onload = () => {
                if (xhr.status === 201 && xhr.response?.id) return resolve();
                const message = xhr.status === 401 || xhr.status === 403 || (xhr.status === 200 && !xhr.response?.id)
                    ? '登录已失效，请在另一个标签页重新登录，再回来重试。'
                    : xhr.status === 413 ? '文件过大或存储已满，请检查文件大小或删除旧内容。'
                    : `保存失败（HTTP ${xhr.status}），请重试。`;
                reject(new Error(message));
            };
            xhr.onerror = () => reject(new Error('网络中断。请先刷新已保存内容确认是否上传成功，再重试。'));
            xhr.ontimeout = () => reject(new Error('上传超时。请先检查已保存内容，再重试。'));
            xhr.send(data);
        });
    }

    form.onsubmit = async event => {
        event.preventDefault();
        if (busy || (!pending.length && !text.value.trim())) return;
        busy = true;
        save.disabled = choose.disabled = picker.disabled = true;
        text.readOnly = true;
        progress.hidden = false;
        render();
        const total = pending.length || 1;
        let completed = 0;
        try {
            do {
                const entry = pending[0];
                const data = new FormData();
                data.append('csrf_token', form.querySelector('[name=csrf_token]').value);
                data.append('text', text.value);
                if (entry) data.append('file', entry.file, entry.file.name);
                await upload(data, completed + 1, total);
                text.value = ''; // Attach the note only to the first successful upload.
                if (entry) {
                    pending.shift();
                    if (entry.url) URL.revokeObjectURL(entry.url);
                }
                completed++;
                render();
            } while (pending.length);
            busy = false;
            window.location.assign(form.action);
        } catch (error) {
            status.textContent = `已保存 ${completed}/${total}。${error.message} 未完成的内容仍保留在这里。`;
        } finally {
            busy = false;
            save.disabled = choose.disabled = picker.disabled = false;
            text.readOnly = false;
            progress.hidden = true;
            render();
        }
    };
    window.addEventListener('beforeunload', event => {
        if (busy || pending.length || text.value.trim()) {
            event.preventDefault();
            event.returnValue = '';
        }
    });
    render();
})();
