import uvicorn

if __name__ == "__main__":
    print("Starting YouTube Subscription Tracker Dashboard...")
    print("Open http://localhost:8000 in your browser.")
    # reload=False in prod: this runs as a long-lived tmux service. The reloader
    # spawns a second filesystem-watching supervisor process and can restart the
    # app mid-poll, dropping the background scheduler thread's state.
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
