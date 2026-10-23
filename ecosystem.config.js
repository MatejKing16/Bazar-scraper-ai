module.exports = {
  apps: [
    {
      name: "bazos-hunter",
      script: "./run.sh",
      interpreter: "bash",
      cwd: "/home/frezator/github/Bazar-scraper-ai",
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 10,
      min_uptime: "30s"
    }
  ]
};
