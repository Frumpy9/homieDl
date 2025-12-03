const jobsContainer = document.getElementById("jobs");
const jobForm = document.getElementById("job-form");
const refreshButton = document.getElementById("refresh");
const configBox = document.getElementById("config");
const streams = new Map();

async function fetchConfig() {
  const res = await fetch("/api/config");
  const data = await res.json();
  configBox.innerHTML = `
    <div><strong>Library</strong>: ${data.library_dir}</div>
    <div><strong>Playlists</strong>: ${data.playlists_dir}</div>
    <div><strong>Template</strong>: ${data.output_template}</div>
    <div><strong>Overwrite</strong>: ${data.overwrite_strategy}</div>
    <div><strong>Threads</strong>: ${data.threads}</div>
  `;
}

function renderJob(job) {
  const trackList = job.tracks
    .map(
      (track) => `
      <li>
        <div class="track-header">
          <span class="title">${track.title}</span>
          <span class="status ${track.status}">${track.status}</span>
        </div>
        <div class="meta">${track.artist}</div>
        ${track.path ? `<div class="path">${track.path}</div>` : ""}
        ${track.message ? `<div class="error">${track.message}</div>` : ""}
      </li>`
    )
    .join("");

  return `
    <div class="job">
      <div class="job-header">
        <div>
          <div class="label">${job.playlist_name || "Untitled"}</div>
          <div class="url">${job.url}</div>
        </div>
        <span class="badge ${job.status}">${job.status}</span>
      </div>
      <ul class="tracks">${trackList}</ul>
      <pre class="logs">${job.logs.join("\n")}</pre>
    </div>
  `;
}

function updateJob(job) {
  const existing = document.querySelector(`[data-job-id="${job.id}"]`);
  const markup = document.createElement("div");
  markup.dataset.jobId = job.id;
  markup.innerHTML = renderJob(job);
  if (existing) {
    existing.replaceWith(markup);
  } else {
    jobsContainer.prepend(markup);
  }
}

async function loadJobs() {
  const res = await fetch("/api/jobs");
  const data = await res.json();
  jobsContainer.innerHTML = "";
  data.forEach((job) => {
    updateJob(job);
    attachStream(job.id);
  });
}

function attachStream(jobId) {
  if (streams.has(jobId)) return;
  const source = new EventSource(`/api/jobs/${jobId}/events`);
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    updateJob(payload.data);
  };
  source.onerror = () => {
    source.close();
    streams.delete(jobId);
  };
  streams.set(jobId, source);
}

jobForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formData = new FormData(jobForm);
  const url = formData.get("url");
  const res = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  const job = await res.json();
  updateJob(job);
  attachStream(job.id);
  jobForm.reset();
});

refreshButton.addEventListener("click", loadJobs);

fetchConfig();
loadJobs();
