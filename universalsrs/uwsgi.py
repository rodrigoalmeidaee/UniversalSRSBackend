def main(global_config, **settings):
    from universalsrs import app
    return app.app.wsgi_app
