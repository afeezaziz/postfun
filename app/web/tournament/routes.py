from flask import render_template

from ...extensions import db
from . import tournament_bp


@tournament_bp.route("/")
def tournament():
    """Tournament Arena page"""
    return render_template("tournament.html")