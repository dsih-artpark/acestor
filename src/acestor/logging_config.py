import logging
import logging.config


def setup_logging(loggers_config=None, default_level='WARNING'):
    """
    Set up logging configuration.

    Args:
        loggers_config (dict): A dictionary where keys are logger names and values are dicts with
                               'level' and 'filename' keys.
        default_level (str): The default logging level ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL').
    """
    default_level = default_level.upper()
    if default_level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        raise ValueError(f"Invalid logging level: {default_level}")

    if loggers_config is None:
        loggers_config = {
            'acestor': {
                'level': default_level,
                'filename': 'acestor.log',
            }
        }

    logging_config = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'standard': {
                'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            },
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'formatter': 'standard',
            },
        },
        'loggers': {
            '': {
                'handlers': ['console'],
                'level': default_level,
                'propagate': True,
            },
        }
    }

    for logger_name, config in loggers_config.items():
        level = config.get('level', default_level).upper()
        filename = config.get('filename', f'{logger_name}.log')

        if level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
            raise ValueError(f"Invalid logging level: {level}")

        handler_name = f'{logger_name}_file'
        logging_config['handlers'][handler_name] = {
            'class': 'logging.FileHandler',
            'formatter': 'standard',
            'filename': filename,
        }

        logging_config['loggers'][logger_name] = {
            'handlers': [ handler_name],
            'level': level,
            'propagate': False,
        }

    logging.config.dictConfig(logging_config)


def set_logging_level(logger_name, level):
    """
    Set the logging level for a specific logger and all its handlers.

    Args:
        logger_name (str): The name of the logger.
        level (str): The logging level to set ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL').
    """
    level = level.upper()
    if level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        raise ValueError(f"Invalid logging level: {level}")

    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    for handler in logger.handlers:
        handler.setLevel(level)

# Example usage:
# setup_logging({
#     'epipipeline': {'level': 'INFO', 'filename': 'epipipeline.log'},
#     'another_logger': {'level': 'DEBUG', 'filename': 'another_logger.log'}
# })
