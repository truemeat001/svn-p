"""Run SVN Desk from a source checkout."""

if __name__ == "__main__":
    # Set DPI awareness before importing/building any native UI.
    from svn_client.display import enable_high_dpi
    enable_high_dpi()
    from svn_client.ui import main
    main()
