from flask import render_template

from ...extensions import db
from . import creator_bp


@creator_bp.route("/")
def creator():
    """Creator Hub page"""
    return render_template("creator.html")