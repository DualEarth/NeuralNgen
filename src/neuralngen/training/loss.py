# src/neuralngen/training/loss.py

import torch
import torch.nn as nn
import torch.nn.functional as F
import random

class ngenLoss(nn.Module):
    """
    Combined loss function:
    - MSE or RMSE
    - Variogram loss (spatial)
    - FDC divergence loss

    Parameters
    ----------
    distance_matrix : torch.Tensor or None
        Pairwise distance matrix (sites x sites). Can be None to skip weighting.
    variogram_weight : float
        Weight for the variogram loss.
    fdc_weight : float
        Weight for the FDC loss.
    v_order : int
        Power for the variogram (default=2 for semi-variance).
    """

    def __init__(
        self,
        distance_matrix=None,
        variogram_weight=0.0,
        fdc_weight=0.0,
        v_order=2
    ):
        super().__init__()

        self.distance_matrix = distance_matrix
        self.variogram_weight = variogram_weight
        self.fdc_weight = fdc_weight
        self.v_order = v_order

    def forward(self, prediction, data):
        """
        Compute total loss.

        Parameters
        ----------
        prediction : Dict[str, torch.Tensor]
            Must contain key "y_hat" [B, T, D]
        data : Dict[str, torch.Tensor]
            Must contain key "y" [B, T, D]

        Returns
        -------
        torch.Tensor
            Scalar loss value.
        dict
            Dictionary of loss components.
        """

        y_hat = prediction["y_hat"]
        y = data["y"]

        # Mask NaNs
        mask = ~torch.isnan(y)
        y_hat = torch.where(mask, y_hat, torch.zeros_like(y_hat))
        y = torch.where(mask, y, torch.zeros_like(y))

        # MSE Loss
        mse_loss = F.mse_loss(y_hat[mask], y[mask])

        loss = mse_loss
        losses = {"mse_loss": mse_loss}

        if self.variogram_weight > 0:
            vario_loss = self._variogram_loss(y_hat, y, mask)
            loss += self.variogram_weight * vario_loss
            losses["variogram_loss"] = vario_loss

        if self.fdc_weight > 0:
            fdc_loss = self._fdc_loss(y_hat, y, mask)
            loss += self.fdc_weight * fdc_loss
            losses["fdc_loss"] = fdc_loss

        losses["total_loss"] = loss

        return loss, losses

    def variogram_loss(self, y_hat, y, mask):
        """
        Compute spatial variogram loss.

        Expects y_hat and y of shape [B, T, D=1]
        """
        method = "quantile"
        y_hat_flat = self.var_hyd_char(y_hat, method=method, 
                                        random_quantile=True).squeeze(-1)
        y_flat = self.var_hyd_char(y, method=method, 
                                      random_quantile=True).squeeze(-1)

        # Compute pairwise |yi - yj|^v
        diff_obs = torch.abs(y_flat[:, None] - y_flat[None, :]) ** self.v_order
        diff_pred = torch.abs(y_hat_flat[:, None] - y_hat_flat[None, :]) ** self.v_order

        loss_matrix = (diff_obs - diff_pred) ** 2

        if self.distance_matrix is not None:
            weight = self.distance_matrix.to(y.device)
            loss_matrix = loss_matrix * weight

        return loss_matrix.mean()

    def fdc_loss(self, y_hat, y, mask):
        """
        Compute FDC divergence loss.
        """

        # Collapse sites and time to a flat vector for FDC
        y_hat_flat = y_hat[mask].flatten()
        y_flat = y[mask].flatten()

        if y_flat.numel() == 0:
            return torch.tensor(0.0, device=y.device)

        y_hat_sorted, _ = torch.sort(y_hat_flat, descending=True)
        y_sorted, _ = torch.sort(y_flat, descending=True)

        min_len = min(len(y_sorted), len(y_hat_sorted))
        y_hat_sorted = y_hat_sorted[:min_len]
        y_sorted = y_sorted[:min_len]

        # Cross variability
        cross_diff = torch.abs(y_sorted[:, None] - y_hat_sorted[None, :])
        cross_var = cross_diff.mean()

        # Within-variability
        within_y = torch.abs(y_sorted[:, None] - y_sorted[None, :]).mean()
        within_hat = torch.abs(y_hat_sorted[:, None] - y_hat_sorted[None, :]).mean()

        fdc_divergence = cross_var - 0.5 * (within_y + within_hat)
        fdc_divergence = torch.clamp(fdc_divergence, min=0.0)

        return fdc_divergence
        
    def var_hyd_char(
        ts: torch.Tensor, 
        method: str = "mean", 
        quantile: float = 0.25,
        random_quantile: bool = False,
        quantile_bounds: tuple = (0.05, 0.95)
    ):
        """
        Compute a single hydrograph characteristic from time series for each basin.

        Parameters
        ----------
        ts : torch.Tensor
            Time series, shape [B, T, D]
        method : str
            Aggregation method. Options:
                - "mean"
                - "median"
                - "std"
                - "quantile"
        quantile : float
            If method == "quantile", the quantile level (0..1).
        random_quantile : bool
            Whether to randomly choose a quantile between quantile_bounds.
        quantile_bounds : tuple(float, float)
            Lower and upper bounds for random quantile.

        Returns
        -------
        torch.Tensor
            Tensor of shape [B, D]
        """

        if method == "mean":
            return ts.mean(dim=1)

        elif method == "median":
            return ts.median(dim=1).values

        elif method == "std":
            return ts.std(dim=1)

        elif method == "quantile":
            if random_quantile:
                quantile = random.uniform(*quantile_bounds)
            sorted_ts, _ = ts.sort(dim=1, descending=True)
            index = int(quantile * ts.shape[1])
            index = max(0, min(index, ts.shape[1] - 1))
            return sorted_ts[:, index, :]
        
        else:
            raise ValueError(f"Unknown method for hydrograph characteristic: {method}")