(function(){
        function setupDropzone(el){
          const input = el.querySelector('input[type="file"]');
          const meta = el.querySelector('.dz-meta');
      
          // Update label on change
          input.addEventListener('change', () => {
            if (input.files && input.files.length){
              const names = Array.from(input.files).map(f => `${f.name} (${Math.round(f.size/1024)} KB)`);
              meta.textContent = names.join(', ');
            } else {
              meta.textContent = '';
            }
          });
      
          // Drag & drop
          el.addEventListener('dragover', (e) => {
            e.preventDefault(); el.classList.add('dragover');
          });
          el.addEventListener('dragleave', () => el.classList.remove('dragover'));
          el.addEventListener('drop', (e) => {
            e.preventDefault(); el.classList.remove('dragover');
            if (e.dataTransfer.files.length){
              input.files = e.dataTransfer.files;
              input.dispatchEvent(new Event('change'));
            }
          });
        }
      
        document.querySelectorAll('.dropzone').forEach(setupDropzone);
      })();
      