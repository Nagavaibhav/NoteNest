const fileInput = document.querySelector('#file');
const fileName = document.querySelector('#file-name');

if (fileInput && fileName) {
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) fileName.textContent = `Selected file: ${fileInput.files[0].name}`;
  });
}

document.querySelectorAll('[data-confirm]').forEach((form) => {
  form.addEventListener('submit', (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
