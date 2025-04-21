from typing import Union
import numpy as np
from acadia import Acadia, DataManager
from acadia_qmsmt import QMsmtRuntime, MeasurableResonator, Qubit, IOConfig
from acadia.runtime import annotate_method

class TestAmpSweepRuntime(QMsmtRuntime):
    """
    A :class:`Runtime` for calibrating the amplitudes of pulses for qubit drives.
    """
    readout_stimulus: IOConfig
    readout_capture: IOConfig

    # Note that these amplitudes override the ``scale`` parameter in the configuration
    amplitudes: Union[list, np.ndarray]

    iterations: int
    run_delay: int


    readout_window_name: str = None
    readout_stimulus_mem: str = None
    readout_stimulus_wf: str = None

    plot: bool = True
    figsize: tuple[int] = (4, 3)
    yaml_path: str = None

    def main(self):
        import logging
        logger = logging.getLogger("acadia")

        readout_stimulus_io = self.io("readout_stimulus")
        readout_capture_io = self.io("readout_capture")

        readout_resonator = MeasurableResonator(readout_stimulus_io, readout_capture_io)

        self.data.add_group(f"points", uniform=True)

        def sequence(a: Acadia):
            readout_resonator.prepare_cmacc(self.readout_window_name)

            with a.channel_synchronizer():
                readout_resonator.measure(self.readout_stimulus_mem, "readout_accumulated")

        self.acadia.compile(sequence)
        self.acadia.attach()
        self.configure_channels()
        self.acadia.assemble()
        self.acadia.load()

        readout_resonator.load_windows()
        readout_stimulus_io.load_waveform(self.readout_stimulus_mem, self.readout_stimulus_wf)

        # Precompute the envelope so that we're not recalculating it every time, only scaling it
        ro_pulse_samples = readout_stimulus_io.compute_waveform(self.readout_stimulus_mem, self.readout_stimulus_wf)

        for i in range(self.iterations):
            for amplitude in self.amplitudes:
                readout_stimulus_io.load_waveform(self.readout_stimulus_mem, ro_pulse_samples, scale=amplitude)
                self.acadia.run(minimum_delay=self.run_delay)
                wf = readout_capture_io.get_waveform_memory("readout_accumulated")
                self.data["points"].write(wf.array)

            if self.data.serve() == DataManager.serve_hangup():
                self.data.disconnect()
                return

        self.final_serve()

    def initialize(self):
        pass

    def update(self):
        # get current completed data
        self.process_current_data()

        # save current data
        self.data.save(self.local_directory)

    def finalize(self):
        super().finalize()
        from acadia_gui.helpers import save_registered_plots
        if self.plot:
            save_registered_plots(self)


    @annotate_method(is_data_processor=True)
    def process_current_data(self):
        # First make sure that we actually have new data to process
        if "points" not in self.data or len(self.data["points"]) < len(self.amplitudes):
            return
        self.completed_iterations = len(self.data["points"]) // len(self.amplitudes)
        valid_points = self.completed_iterations * len(self.amplitudes)

        data = self.data["points"].records()[:valid_points, ...]
        data = data.reshape(self.completed_iterations, len(self.amplitudes), 2)
        self.data_iq = data.astype(float).view(complex).squeeze()
        self.avg_iq = np.sum(self.data_iq, axis=0)
        return self.completed_iterations


    @annotate_method(plot_name="mag_vs_dac", axs_shape=(1,1))
    def plot_data_mag(self, axs=None):
        import matplotlib.pyplot as plt
        if axs is None:
            fig, axs = plt.subplots(1, 1, figsize=self.figsize)
        else:
            fig, axs = axs.figure, axs
        axs.plot(self.amplitudes, np.abs(self.avg_iq), label="mag")
        axs.legend()
        axs.set_xlabel(max(np.abs(self.avg_iq))) # to show this will not stack up
        fig.tight_layout()
        return fig, axs


    @annotate_method(plot_name="iq_vs_dac", axs_shape=(2,1))
    def plot_data_iq(self, axs=None):
        from acadia_gui.helpers import prepare_axes
        fig, axs = prepare_axes(axs, axs_shape=(2,1), figsize=self.figsize)
        axs[0].plot(self.amplitudes, self.avg_iq.real)
        axs[1].plot(self.amplitudes, self.avg_iq.imag)
        fig.tight_layout()
        return fig, axs
