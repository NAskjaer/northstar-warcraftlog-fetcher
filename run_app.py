import subprocess
import sys
import os

def main():
    # Ensure we're in the project root (where .streamlit, ui/, etc. live)
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    # Run: streamlit run ui/app.py
    subprocess.run([sys.executable, "-m", "streamlit", "run", "ui/app.py"])

if __name__ == "__main__":
    main()
