// Browser-selected file paths are identifiers, never server filesystem paths.
function indexFolderFiles(files) {
  const index = new Map();
  for (const file of files) {
    if (!file.webkitRelativePath) continue;
    const path = file.webkitRelativePath.normalize('NFC');
    if (index.has(path)) {
      throw new Error('所选文件夹中存在无法区分的同名路径（Unicode 编码不同或路径重复），请重命名后重试：' + path);
    }
    index.set(path, file);
  }
  return index;
}

if (typeof module !== 'undefined' && module.exports) module.exports = {indexFolderFiles};
