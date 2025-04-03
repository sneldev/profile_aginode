# -*- coding: utf-8 -*-
# Copyright 2020 CorTex IT Solutions Ltd. (<https://cortexsolutions.net/>)
# License OPL-1

from odoo import models, fields, _, api, Command
from odoo.exceptions import UserError
from dateutil.relativedelta import relativedelta


class AccountMove(models.Model):
    _inherit = 'account.move'

    # Override deferred account
    def _generate_deferred_entries(self):
        """
        Generates the deferred entries for the invoice.
        """
        self.ensure_one()
        if self.is_entry():
            raise UserError(_("You cannot generate deferred entries for a miscellaneous journal entry."))
        deferred_type = "expense" if self.is_purchase_document() else "revenue"
        deferred_account = self.company_id.deferred_expense_account_id if deferred_type == "expense" else self.company_id.deferred_revenue_account_id
        deferred_journal = self.company_id.deferred_expense_journal_id if deferred_type == "expense" else self.company_id.deferred_revenue_journal_id
        if not deferred_journal:
            raise UserError(_("Please set the deferred journal in the accounting settings."))
        if not deferred_account:
            raise UserError(_("Please set the deferred accounts in the accounting settings."))

        for line in self.line_ids.filtered(lambda l: l.deferred_start_date and l.deferred_end_date):
            periods = line._get_deferred_periods()
            if not periods:
                continue

            # This below two line added by Waleed to use deferred account for each line
            if line.deferred_account_id:
                deferred_account = line.deferred_account_id

            ref = _("Deferral of %s", line.move_id.name or '')

            move_vals = {
                'move_type': 'entry',
                'deferred_original_move_ids': [Command.set(line.move_id.ids)],
                'journal_id': deferred_journal.id,
                'company_id': self.company_id.id,
                'partner_id': line.partner_id.id,
                'auto_post': 'at_date',
                'ref': ref,
            }

            # Defer the current invoice
            move_fully_deferred = self.create({
                **move_vals,
                'date': line.move_id.date,
            })
            # We write the lines after creation, to make sure the `deferred_original_move_ids` is set.
            # This way we can avoid adding taxes for deferred moves.
            move_fully_deferred.write({
                'line_ids': [
                    Command.create(
                        self.env['account.move.line']._get_deferred_lines_values(account.id, coeff * line.balance, ref,
                                                                                 line.analytic_distribution, line)
                    ) for (account, coeff) in [(line.account_id, -1), (deferred_account, 1)]
                ],
            })

            # Create the deferred entries for the periods [deferred_start_date, deferred_end_date]
            deferral_moves = self.create([{
                **move_vals,
                'date': period[1],
            } for period in periods])
            remaining_balance = line.balance
            for period_index, (period, deferral_move) in enumerate(zip(periods, deferral_moves)):
                # For the last deferral move the balance is forced to remaining balance to avoid rounding errors
                force_balance = remaining_balance if period_index == len(periods) - 1 else None
                # Same as before, to avoid adding taxes for deferred moves.
                deferral_move.write({
                    'line_ids': self._get_deferred_lines(line, deferred_account, deferred_type, period, ref,
                                                         force_balance=force_balance),
                })
                remaining_balance -= deferral_move.line_ids[0].balance
                # Avoid having deferral moves with a total amount of 0
                if deferral_move.currency_id.is_zero(deferral_move.amount_total):
                    deferral_moves -= deferral_move
                    deferral_move.unlink()

            deferred_moves = move_fully_deferred + deferral_moves
            if len(deferral_moves) == 1 and move_fully_deferred.date.month == deferral_moves.date.month:
                # If, after calculation, we have 2 deferral entries in the same month, it means that
                # they simply cancel out each other, so there is no point in creating them.
                deferred_moves.unlink()
                continue
            line.move_id.deferred_move_ids |= deferred_moves
            deferred_moves._post(soft=True)
