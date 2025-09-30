from flask import render_template

from ...extensions import db
from . import reward_bp


@reward_bp.route("/")
def reward():
    """Reward Center page"""
    return render_template("reward.html")