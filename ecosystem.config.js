module.exports = {
  apps: [
    {
      name: "bazos-hunter",
      script: "./run.sh",
      interpreter: "bash",
      cwd: "/home/frezator/github/Bazar-scraper-ai",
      autorestart: true,
      restart_delay: 5000,
      exp_backoff_restart_delay: 1000,
      min_uptime: "20s",
      kill_timeout: 5000,
      max_restarts: 50
    }
  ]
};
